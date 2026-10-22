# 发行包

运行 `python scripts/pack_release.py` 生成 Rust / Vue v2 源码发行包；`--no-zip` 仅生成目录。

内容包含 Rust、SQL 迁移、Vue 源码与锁文件、Docker 配置、旧数据库导出工具和 v2 文档。不会打包 `.env`、数据库、密钥、node_modules 或编译缓存。发行目录采用带日期的新名称，不覆盖已有发行包。

从包内 `开始使用.md` 开始，使用 Docker 构建后初始化账户。QQ 群向量库使用独立的 Qdrant 数据卷。旧版安装包与运行数据应单独保留。
