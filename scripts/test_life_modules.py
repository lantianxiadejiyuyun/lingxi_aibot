"""健身 / 出行 / 消费：AI 工具 CRUD、一键重置覆盖三表、App API Token 按用户隔离。"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta

from flask_login import login_user

from run import app
from app.ai import registry
from app.ai.registry import load_tools
from app.extensions import db
from app.models.expense import ExpenseRecord
from app.models.fitness import FitnessRecord
from app.models.travel import TripPlan
from app.models.user import User
from app.services.data_service import BUSINESS_TABLES
from app.services.install_service import ensure_schema
from app.utils.api_auth import assign_user_api_token, revoke_user_api_token

PASSED, FAILED = [], []
MARK = "[test-life]"


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


def _login(uid: int):
    user = db.session.get(User, uid)
    login_user(user)
    return user


def tool(name, args=None):
    """执行工具：JSON 结果反序列化，纯文本原样返回。"""
    raw = registry.execute_tool(name, args or {})
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


with app.app_context():
    ensure_schema()
    load_tools()
    leftover = User.query.filter(User.username.like("test_life_other_%")).all()
    for u in leftover:
        db.session.delete(u)
    if leftover:
        db.session.commit()

    admin = User.query.filter_by(is_admin=True).order_by(User.id).first() or User.query.order_by(User.id).first()
    check("存在可用用户", admin is not None)
    if admin is None:
        print("无法继续")
        sys.exit(1)

    names = set(registry.tool_names())
    for t in (
        "list_fitness_records", "create_fitness_record", "update_fitness_record",
        "delete_fitness_record", "analyze_fitness",
        "list_trip_plans", "get_trip_plan", "create_trip_plan",
        "update_trip_plan", "generate_trip_itinerary", "delete_trip_plan",
        "list_expenses", "get_expense_stats", "create_expense",
        "update_expense", "delete_expense",
    ):
        check(f"已注册工具 {t}", t in names)

    check("重置表含健身/出行/消费",
          "fitness_records" in BUSINESS_TABLES
          and "trip_plans" in BUSINESS_TABLES
          and "expense_records" in BUSINESS_TABLES)

    with app.test_request_context("/"):
        _login(admin.id)
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=2)).isoformat()

        fit = tool("create_fitness_record", {
            "duration_min": 30, "workout_type": "跑步", "date": today,
            "notes": MARK,
        })
        check("create_fitness_record 成功", isinstance(fit, str) and "已记录训练" in fit, fit)
        listed = tool("list_fitness_records", {"start_date": today, "end_date": today})
        recs = listed.get("records") if isinstance(listed, dict) else []
        fit_id = next((r["id"] for r in recs if r.get("notes") == MARK), None)
        check("list_fitness_records 能看到新建", fit_id is not None, str(listed)[:200])
        if fit_id:
            upd = tool("update_fitness_record", {"record_id": fit_id, "duration_min": 45})
            check("update_fitness_record", isinstance(upd, dict) and upd.get("duration_min") == 45, str(upd)[:120])
            analysis = tool("analyze_fitness", {})
            check("analyze_fitness 有内容", isinstance(analysis, str) and len(analysis) > 8, str(analysis)[:80])
            deleted = tool("delete_fitness_record", {"record_id": fit_id})
            check("delete_fitness_record", isinstance(deleted, str) and "已删除" in deleted, deleted)

        trip = tool("create_trip_plan", {
            "destination": f"{MARK}杭州",
            "start_date": today,
            "end_date": tomorrow,
            "transports": ["高铁"],
            "notes": MARK,
        })
        check("create_trip_plan 成功", isinstance(trip, str) and "已创建出行计划" in trip, trip)
        plans = tool("list_trip_plans", {})
        plan_id = None
        if isinstance(plans, list):
            plan_id = next((p["id"] for p in plans if p.get("destination") == f"{MARK}杭州"), None)
        check("list_trip_plans 能看到新建", plan_id is not None, str(plans)[:200])
        if plan_id:
            detail = tool("get_trip_plan", {"plan_id": plan_id})
            check("get_trip_plan", isinstance(detail, dict) and detail.get("id") == plan_id, str(detail)[:120])
            upd = tool("update_trip_plan", {"plan_id": plan_id, "budget": 1200})
            check("update_trip_plan", isinstance(upd, dict) and upd.get("budget") == 1200, str(upd)[:120])
            deleted = tool("delete_trip_plan", {"plan_id": plan_id})
            check("delete_trip_plan", isinstance(deleted, str) and "已删除" in deleted, deleted)

        exp = tool("create_expense", {
            "amount": 12.5, "category": "餐饮", "date": today,
            "payment_method": "微信", "notes": MARK,
        })
        check("create_expense 成功", isinstance(exp, str) and "已记账" in exp, exp)
        items = tool("list_expenses", {"start_date": today, "end_date": today, "category": "餐饮"})
        exp_id = None
        if isinstance(items, list):
            exp_id = next((r["id"] for r in items if r.get("notes") == MARK), None)
        check("list_expenses 能看到新建", exp_id is not None, str(items)[:200])
        stats = tool("get_expense_stats", {"start_date": today, "end_date": today})
        check("get_expense_stats", isinstance(stats, dict) and "total" in stats, str(stats)[:120])
        if exp_id:
            upd = tool("update_expense", {"record_id": exp_id, "amount": 18})
            check("update_expense", isinstance(upd, dict) and float(upd.get("amount") or 0) == 18, str(upd)[:120])

        # Token 按用户隔离：给 admin 发 Token，用测试客户端调 API
        token = assign_user_api_token(admin)
        client = app.test_client()
        headers = {"X-API-Token": token, "Content-Type": "application/json"}
        r = client.get("/api/v1/expenses", headers=headers)
        check("用户 Token 可访问消费 API", r.status_code == 200 and (r.get_json() or {}).get("ok"), r.status)
        r = client.get("/api/v1/expenses", headers={"X-API-Token": "lx_not-a-real-token"})
        check("错误 Token 401", r.status_code == 401)

        other = User(username=f"test_life_other_{os.getpid()}", timezone="Asia/Shanghai")
        other.set_password("test-life-other-pass")
        db.session.add(other)
        db.session.commit()
        other_token = assign_user_api_token(other)
        r = client.post("/api/v1/expenses", headers={
            "X-API-Token": other_token, "Content-Type": "application/json",
        }, json={"amount": 3.3, "category": "其他", "date": today, "notes": MARK + "other"})
        check("其他用户 Token 可记账", r.status_code in (200, 201) and (r.get_json() or {}).get("ok"), r.text[:160])
        other_json = r.get_json() or {}
        other_exp_id = (other_json.get("data") or {}).get("id")

        admin_list = client.get("/api/v1/expenses?start_date=%s&end_date=%s" % (today, today),
                                headers=headers).get_json() or {}
        admin_ids = {row["id"] for row in (admin_list.get("data") or [])}
        check("admin Token 看不到他人账单", other_exp_id not in admin_ids,
              f"other={other_exp_id} admin_ids={list(admin_ids)[:8]}")

        if other_exp_id:
            sneak = client.get(f"/api/v1/expenses/{other_exp_id}", headers=headers)
            check("admin Token 读他人详情 404", sneak.status_code == 404, str(sneak.status_code))

        # 清理
        if exp_id:
            tool("delete_expense", {"record_id": exp_id})
        if other_exp_id:
            rec = db.session.get(ExpenseRecord, other_exp_id)
            if rec is not None:
                db.session.delete(rec)
                db.session.commit()
        FitnessRecord.query.filter(FitnessRecord.notes == MARK).delete()
        TripPlan.query.filter(TripPlan.notes == MARK).delete()
        ExpenseRecord.query.filter(ExpenseRecord.notes.like(MARK + "%")).delete()
        db.session.commit()
        revoke_user_api_token(admin)
        db.session.delete(other)
        db.session.commit()

print(f"\n通过 {len(PASSED)}  失败 {len(FAILED)}")
if FAILED:
    print("失败项：", ", ".join(FAILED))
    sys.exit(1)
