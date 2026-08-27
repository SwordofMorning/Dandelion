# Architecture of Dandelion

本文档旨在划定整个dandelion工程的架构，重点讲述`src`和`mk`的机制，这可以让你快速了解本工程的调用结构、构建逻辑。

## 一、主体架构

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

