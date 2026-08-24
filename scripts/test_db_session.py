"""失败事务必须回滚，后续 get_setting / 设置页不能再 PendingRollbackError。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from sqlalchemy.exc import PendingRollbackError

from run import app
from app.extensions import db, recover_session
from app.services.settings_service import get_setting, get_setting_from

PASSED, FAILED = [], []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


def _poison():
    try:
        db.session.execute(text("SELECT * FROM __lingxi_no_such_table__"))
        db.session.commit()
    except Exception:  # noqa: BLE001 —— 故意制造失败事务
        pass


with app.app_context():
    _poison()
    recover_session()
    try:
        db.session.execute(text("SELECT 1"))
        ok = True
    except PendingRollbackError:
        ok = False
    check("recover_session 后可继续查询", ok)

    _poison()
    try:
        val = get_setting("backup_enabled", True, user_id=0)
        ok = True
    except PendingRollbackError as e:
        ok = False
        val = e
    check("get_setting 遇失败事务自动恢复", ok, str(val))

    _poison()
    try:
        val = get_setting_from("backup_enabled", None, True, user_id=0)
        ok = True
    except PendingRollbackError as e:
        ok = False
        val = e
    check("get_setting_from 遇失败事务自动恢复", ok, str(val))

    c = app.test_client()
    _poison()
    r = c.get("/settings/", follow_redirects=False)
    check("设置页不会因失败事务 500", r.status_code in (200, 302), f"status={r.status_code}")

print(f"\n通过 {len(PASSED)}  失败 {len(FAILED)}")
if FAILED:
    print("失败项：", ", ".join(FAILED))
    sys.exit(1)
