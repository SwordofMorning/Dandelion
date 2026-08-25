# Architecture of Dandelion

本文档旨在划定整个dandelion工程的架构，重点讲述`src`和`mk`的机制，这可以让你快速了解本工程的调用结构、构建逻辑。

## 一、主体架构

### 1.1 概览

暂时忽略`.gitignore`、`.env`等“辅助文件”，dandelion可以被最简单地概括为：

```sh
1. main.py      # main() 函数入口
    -> src/         # 各个子模块的源码
2. mk/          # 编译构建脚本，同时`mk/lib/paths.py`还用于向 main.py 提供PWD路径定位
```

加上其他的文件夹，那么工程可以表示为：

```sh
1. main.py      # main() 函数入口
    -> src/         # 各个子模块的源码
    -> llm/         # 保存skill、全局持久化记忆
    -> .log/        # 保存会话、日志
    -> .env/        # 存储API Key、routing、SSH config等配置
2. mk/          # 编译构建脚本，同时`mk/lib/paths.py`还用于向 main.py 提供PWD路径定位
3. test/        # 测试代码/用例
4. docs/        # 项目文档
5. .github/     # GHA workflow
```

### 1.2 `main.py`

`main.py`代码相对简洁，其中主要做如下几件事：

1. 为sandbox定位当前的pwd，避免dandelion潜在的击穿沙箱的风险。
    > 换句话说，dandelion只默认允许对当前pwd下的文件进行操作。当然，你可以可以通过在其他位置，或者配置到环境变量中来调用dandelion来解除这一限制。
2. 加载`.env/`下的配置文件；
3. 初始化会话管理器；
4. 依据配置内容构造Main Agent；
5. 将Agent和会话管理器赋值给CLI并启动。

## 二、`mk/`

`mk/`相对简单，并且`mk/lib/paths.py`还为程序提供了pwd定位的工作，因此我们先从项目的构建流程入手。如果你对工程构建没有兴趣，可以直接跳转到[下一章](#三srcutils)。

构建工具选用Nuitka，只对项目中的“小文件”进行编译；较大的文件（例如，Google的一系列SDK）则采用拷贝Python源码的方式添加到构建产物中。

对于本地的构建，可以直接使用命令：

```bash
#   python mk/make.py            : full build (same as 'all')
#   python mk/make.py clean      : remove runtime caches and build artifacts
#   python mk/make.py all        : build standalone executable into build/
#   python mk/make.py version    : print project name and version
#   python mk/make.py sign       : sign Windows executable (placeholder)
```

其输结果将输出到`build`文件夹下。

对于GHA构建，则通过`.github/workflows/release.yml -> mk/make.py`的顺序来调用。

### 2.1 mk的架构

`mk`文件夹下主要有如此的内容：

```sh
.
├── config                  # 配置文件夹
│   ├── build.config            # Nuitka 配置文件
│   └── dandelion.ico           # 二进制程序图标
├── __init__.py             # 导出包，主要用于main.py读取
├── lib                     # 辅助函数
│   ├── build_info.py           # 仅用于添加一些常量，用于打印
│   ├── __init__.py             # 导出包
│   └── paths.py                # 为`源码`或者`二进制`程序定位PWD
└── make.py                 # 构建主函数
```

### 2.2 Nuitka配置

`mk/config/build.config`中定义了一些基本配置，其中值得注意的是：

```sh
# Nuitka config: Do not compile these heavy SDKs and libraries to C.
nofollow_imports = pydantic,anthropic,openai,google,pandas,openpyxl,tabulate,httpx,anyio,certifi,urllib3,idna,charset_normalizer,requests

# Custom config: Manually copy these packages to the dist folder (bypassing Nuitka).
copy_packages = pydantic,anthropic,openai,google,pandas,openpyxl,tabulate,httpx,anyio,certifi,urllib3,idna,charset_normalizer,requests

# Custom config: Force compile standard libraries required by the bypassed packages.
include_std_modules = http.cookies,http.cookiejar,email.parser,email.message,html.parser,urllib.parse,urllib.request,urllib.error,csv,ctypes,sqlite3
```

为了减少编译时间，对上述的package指定了不编译与手动拷贝。如果需要“全编译”，可以删除这几行配置。

同时，配置中的：

```sh
windows_jobs = 2
```

这里是为了避免GHA的Windows平台潜在的内存不足，而手动限制`make -j`的任务数量 - 针对大的Python文件，还是指Google的SDK。

### 2.3 make.py

在文件的最下方的`main()`中，根据用户的输入进行不同的函数分发，对于核心构建函数`target_all()`来说，它按顺序执行如下的三个工作：

#### 2.3.1 前置信息

1. 读取元数据：通过`_project_meta()`从`pyproject.toml`中读取项目的名称、版本号；如果是GHA环境，则读取git tag以获取精确的版本号。
2. 生成静态信息：通过`_gen_build_info()`生成`mk/lib/build_info.py`，该文件将编译时间、版本等放入其中作为常量，以类似于C/C++中`.h`的方式暴露给`main.py`，例如：
    ```py
        VERSION = "0.0.0-dev-260807-abc123-ci"
        BUILD_MODE = "standalone"
        BUILD_DATE = "2026-08-10 09:30:28"
        PLATFORM = "linux"
    ```

#### 2.3.2 核心编译

对于函数`_run_nuitka()`来说，其核心功能是将Python代码转换为二进制产物。对于这个函数来说：

1. 首先读取相应的配置，对于Windows平台，则确保其`make -j`应该为2，避免内存溢出；
2. 显式的配置`anti-bloat`，去除`pytest/unittest`等不必要的测试库；
3. 通过`nofollow_imports`明确Nuitka不应该追踪哪些大文件；
4. 通过`include_std_modules`引入哪些包应该必要包含，避免后期C编译时的错误
5. 最后通过`subprocess.run`来调用Nuitka，生成构建后产物。

#### 2.3.3 后处理

由于在上一节中，为了节约编译时间和资源，我们手动排除了一些库，因此在这里我们需要重新将其引入到构建产物中。我们通过函数`_postprocess()`来实现这些工作。

1. 目录迁移，将Nuitka默认生成的`<entry>.dist`，重命名并拷贝到`build/Dandelion/bin`下；
    > 外层是工作区，将bin文件包裹在内层。
2. 将之前排除的依赖项重新拷贝到`bin`中；
    > `_copy_dependency_closure()`利用BFS搜索相应的库、被其调用的底层库、以及`.dist-info`，确保运行时不会出现版本校验错误。
3. 重新引入标准库；
    > 以`requests`为例，假如我们让Nuitka忽略构建这个库，而这个库用到了`ssl`。如果我们在核心代码中没有用到`ssl`，那么可能出现遗失的问题。
    > 因此，我们利用Python内置的`ast`扫描在(2)中拷贝的依赖，然后将其中被使用的标准库放入`_stdlib_fallback`目录中。
4. 构建工作区；
    > 为例实现“开箱即用”，因此在`build/Dandelion`“根目录”下创建`.env`、`llm/memory`、`llm/skill`和`.log/`等文件夹，并将`.sample`拷贝入其中并移除后缀。
5. 生成运行脚本；
    > 为了可以在`build/Dandelion`这一层中运行程序，而不需要进到`build/Dandelion/bin`中，在外层放置了`dandelion.cmd`和`dandelion.sh`，用于执行`bin`中的实际二进制程序。

## 三、`src/utils/`

