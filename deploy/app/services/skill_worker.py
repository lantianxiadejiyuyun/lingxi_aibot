"""技能子进程执行器：从 stdin 读 JSON，受限编译后执行，结果 JSON 写到 stdout。

由 skill_service._run_in_subprocess 通过 `python skill_worker.py` 启动。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # 项目根（app/services → 上三级）


def main() -> None:
    payload = json.loads(sys.stdin.read())
    from app.services.skill_service import compile_skill_fn

    try:
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
    sys.stdout.write(json.dumps(out, ensure_ascii=False, default=str))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
