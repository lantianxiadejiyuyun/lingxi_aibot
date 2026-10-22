# v1 资料归档

此目录保留 Python / Flask v1 的历史说明、部署配置和辅助镜像文件，仅供迁移参考。它不包含可运行的旧后端，也不参与当前构建；其中的命令、路径、配置变量与功能说明不适用于 Rust / Vue v2。

完整旧实现保存在 [Git 提交 2f8915f](https://github.com/lantianxiadejiyuyun/lingxi_aibot/tree/2f8915f)。如需重现旧版本，应使用该提交的完整独立 checkout 和原始备份，不要把此目录的编排叠加到新版服务。

当前安装、QQ 接入与数据迁移请从仓库根目录的 README 和 `docs/Rust部署与迁移.md` 开始。Python 导出工具在 `scripts/export_legacy.py`，AstrBot 插件在 `integrations/astrbot`；两者与已移除的 Flask 后端无关。
