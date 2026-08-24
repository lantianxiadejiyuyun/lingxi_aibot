"""flask CLI：reset-db / reset-admin-password（不真实 DROP 生产表）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run import app
from app import commands
from app.extensions import db
from app.models.user import User
from app.services import install_service as inst

PASSED, FAILED = [], []
MARK = "_cli_test_user_"


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


orig_drop = inst.drop_all_tables
inst.drop_all_tables = lambda: (_ for _ in ()).throw(AssertionError("测试不得真实删表"))
runner = app.test_cli_runner()

try:
    with app.app_context():
        result = runner.invoke(commands.reset_db, [])
        out = (result.output or "") + str(result.exception or "")
        check("reset-db 无 --yes 不删除", result.exit_code != 0 and "--yes" in out,
              f"code={result.exit_code} out={out[:200]}")
        check("reset-db 无 --yes 会列出表名", "将删除" in (result.output or "")
              or "没有数据表" in (result.output or ""),
              result.output)

        dropped = []

        def _fake_drop():
            dropped.append(True)
            return ["users", "events"]

        inst.drop_all_tables = _fake_drop
        result = runner.invoke(commands.reset_db, ["--yes"])
        check("reset-db --yes 调用删表", result.exit_code == 0 and dropped
              and "2 张" in (result.output or ""),
              f"code={result.exit_code} out={result.output}")
        inst.drop_all_tables = lambda: (_ for _ in ()).throw(
            AssertionError("测试不得真实删表"))

        uname = MARK + "admin"
        other = MARK + "user"
        for leftover in User.query.filter(User.username.in_([uname, other])).all():
            db.session.delete(leftover)
        db.session.commit()

        admin = User(username=uname, timezone="Asia/Shanghai", is_admin=True)
        admin.set_password("old-cli-pass-1")
        member = User(username=other, timezone="Asia/Shanghai", is_admin=False)
        member.set_password("old-cli-pass-2")
        db.session.add_all([admin, member])
        db.session.commit()
        admin_id, member_id = admin.id, member.id

        try:
            result = runner.invoke(commands.reset_admin_password, [
                "--username", "definitely-not-exist-cli-xx", "--password", "abcdef"])
            out = (result.output or "") + str(result.exception or "")
            check("密码重置：用户不存在报错", result.exit_code != 0 and "不存在" in out,
                  f"code={result.exit_code} out={out[:200]}")

            result = runner.invoke(commands.reset_admin_password, [
                "--username", other, "--password", "abcdef"])
            out = (result.output or "") + str(result.exception or "")
            check("密码重置：非管理员报错", result.exit_code != 0 and "不是管理员" in out,
                  f"code={result.exit_code} out={out[:200]}")

            result = runner.invoke(commands.reset_admin_password, [
                "--username", uname, "--password", "123"])
            out = (result.output or "") + str(result.exception or "")
            check("密码重置：密码过短报错", result.exit_code != 0 and "至少 6" in out,
                  f"code={result.exit_code} out={out[:200]}")

            result = runner.invoke(commands.reset_admin_password, [
                "--username", uname, "--password", "new-cli-pass-9"])
            check("密码重置：指定密码成功", result.exit_code == 0
                  and uname in (result.output or ""),
                  f"code={result.exit_code} out={result.output}")
            db.session.expire_all()
            fresh = db.session.get(User, admin_id)
            check("指定密码可以登录校验",
                  fresh is not None and fresh.check_password("new-cli-pass-9"))

            result = runner.invoke(commands.reset_admin_password, ["--username", uname])
            out = result.output or ""
            check("密码重置：省略密码则打印随机密码",
                  result.exit_code == 0 and "随机密码" in out,
                  f"code={result.exit_code} out={out}")
            generated = ""
            for line in out.splitlines():
                if "随机密码" in line and "：" in line:
                    generated = line.split("：", 1)[-1].strip()
                    break
            db.session.expire_all()
            fresh = db.session.get(User, admin_id)
            check("随机密码可以登录校验",
                  len(generated) >= 6 and fresh is not None
                  and fresh.check_password(generated),
                  f"generated={generated!r}")

            result = runner.invoke(commands.reset_admin_password, [])
            out = (result.output or "") + str(result.exception or "")
            n_admin = User.query.filter_by(is_admin=True).count()
            if n_admin == 1:
                check("省略用户名且仅一名管理员", result.exit_code == 0,
                      f"code={result.exit_code} out={out[:200]}")
            else:
                check("多名管理员必须指定用户名",
                      result.exit_code != 0 and "--username" in out,
                      f"n={n_admin} code={result.exit_code} out={out[:200]}")
        finally:
            db.session.expire_all()
            for u in User.query.filter(User.username.in_([uname, other])).all():
                db.session.delete(u)
            db.session.commit()
finally:
    inst.drop_all_tables = orig_drop

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：", FAILED)
    sys.exit(1)
