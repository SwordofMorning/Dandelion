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

### 1.2 main.py

`main.py`代码相对简洁，其中主要做如下几件事：

1. 为sandbox定位当前的pwd，避免dandelion潜在的击穿沙箱的风险。
    > 换句话说，dandelion只默认允许对当前pwd下的文件进行操作。当然，你可以可以通过在其他位置，或者配置到环境变量中来调用dandelion来解除这一限制。
2. 加载`.env/`下的配置文件；
3. 初始化会话管理器；
4. 依据配置内容构造Main Agent；
5. 将Agent和会话管理器赋值给CLI并启动。

## 二、mk

`mk/`相对简单，并且`mk/lib/paths.py`还为程序提供了pwd定位的工作，因此我们先从项目的构建流程入手。