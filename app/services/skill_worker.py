"""技能子进程执行器：从 stdin 读 JSON，受限编译后执行，结果 JSON 写到 stdout。

由 skill_service._run_in_subprocess 通过 `python skill_worker.py` 启动。
子进程内创建最小 Flask 应用上下文（config + db），技能内注入的业务服务才能正常工作。
Linux 下用 resource 限制内存（RLIMIT_AS）与 CPU（RLIMIT_CPU），防止技能内存炸弹
OOM 宿主或无限消耗 CPU；非 Linux 平台（如 Windows 开发机）try/except 跳过。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # 项目根（app/services → 上三级）

# 内存上限 512MB：防技能分配超大列表/字符串 OOM 宿主
_MEM_LIMIT = 512 * 1024 * 1024


def _apply_resource_limits() -> None:
    """Linux 下限制本进程资源；resource 不可用的平台（Windows/macOS 部分情况）静默跳过。"""
    try:
        import resource
    except ImportError:
        return
    try:
        from app.services.skill_service import SKILL_TIMEOUT
    except ImportError:
        return
    try:
        # 虚拟地址空间上限：超限时内存分配抛 MemoryError（防内存炸弹 OOM 宿主）
        resource.setrlimit(resource.RLIMIT_AS, (_MEM_LIMIT, _MEM_LIMIT))
        # CPU 时间上限：与主进程超时一致（略留余量覆盖 Flask/SQLAlchemy 导入开销，
        # 主进程墙钟超时仍兜底强杀）；超限由 SIGXCPU 终止进程
        cpu_limit = SKILL_TIMEOUT + 5
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
    except (ValueError, OSError):
        pass  # 平台不支持该资源限制则跳过


def main() -> None:
    _apply_resource_limits()
    # 主进程始终以 UTF-8 发送 payload（input=payload.encode("utf-8")）；
    # 显式按 UTF-8 读 stdin 字节，避免 Windows 等平台按本地编码（如 cp936）
    # 解码导致中文/多字节内容损坏
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))

    # 最小应用上下文：只加载 Config 与 db，不启动调度器/蓝图（保持子进程轻量）
    from flask import Flask

    from app.config import Config
    from app.extensions import db

    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    from app.services.skill_service import compile_skill_fn
    from app.utils.scoping import set_current_user_id

    try:
        uid = payload.get("user_id") or 0
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            uid = 0
        set_current_user_id(uid)
        with app.app_context():
            fn = compile_skill_fn(payload["name"], payload["parameters"], payload["code"])
            result = fn(**payload.get("args") or {})
        try:
            json.dumps(result)
        except TypeError:
            result = {"error": f"返回值不可序列化: {type(result).__name__}"}
        out = {"result": result}
    except ValueError as e:
        out = {"error": f"ValueError: {e}"}
    except Exception as e:  # noqa: BLE001 —— 错误信息回传主进程
        out = {"error": f"{type(e).__name__}: {e}"}
    # 主进程按 UTF-8 解码 stdout；显式 UTF-8 写出，避免平台编码损坏中文
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False, default=str).encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
