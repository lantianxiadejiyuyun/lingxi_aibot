"""技能沙箱升级测试：模板工具 / 良性技能子进程执行 / 死循环超时强杀 / ValueError 回传。"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

BASE = "http://127.0.0.1:5000"
PASSED, FAILED = [], []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


from run import app
from app.services import skill_service
from app.ai import registry

with app.app_context():
    # 预清理上次残留
    for sk in skill_service.list_skills():
        if sk.name.startswith("sandbox"):
            skill_service.soft_delete(sk)

    # 1. 模板工具
    tpl = registry.execute_tool("get_skill_template", {})
    check("模板工具可用", isinstance(tpl, str) and "参数签名" in tpl and "示例" in tpl, str(tpl)[:60])
    check("模板含可用服务", "calendar_service" in tpl)

    # 2. 良性技能：创建→启用→子进程执行
    skill = skill_service.create_skill(
        "sandbox_echo", "测试技能：原样返回前缀 echo",
        {"type": "object", "properties": {"msg": {"type": "string"}},
         "required": ["msg"]},
        "return 'echo:' + msg")
    skill_service.set_enabled(skill, True)
    result = registry.execute_tool("sandbox_echo", {"msg": "hi"})
    check("良性技能子进程执行", result == "echo:hi", f"result={result!r}")

    # 3. 静态扫描仍拒绝 while True
    rejected = False
    try:
        skill_service.create_skill("sandbox_bad1", "死循环",
                                   {"type": "object", "properties": {}, "required": []},
                                   "while True:\n    pass")
    except skill_service.SkillError:
        rejected = True
    check("静态扫描拒绝 while True", rejected)

    # 4. 绕过静态扫描的死循环 → 子进程超时强杀
    loop = skill_service.create_skill(
        "sandbox_loop", "测试死循环（应被超时强杀）",
        {"type": "object", "properties": {}, "required": []},
        "x = 1\nwhile x == 1:\n    pass")
    skill_service.set_enabled(skill_service.get_skill(loop.id), True)
    import time
    t0 = time.time()
    res = registry.execute_tool("sandbox_loop", {})
    elapsed = time.time() - t0
    check("死循环被超时强杀", isinstance(res, str) and "超时" in res, f"{res!r} {elapsed:.1f}s")
    check("超时耗时合理(<15s)", elapsed < 15, f"{elapsed:.1f}s")

    # 5. ValueError 回传
    err_skill = skill_service.create_skill(
        "sandbox_err", "测试抛错",
        {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
        "if n < 0:\n    raise ValueError(\"n 不能为负数\")\nreturn n * 2")
    skill_service.set_enabled(skill_service.get_skill(err_skill.id), True)
    res2 = registry.execute_tool("sandbox_err", {"n": -5})
    check("ValueError 信息回传", isinstance(res2, str) and "n 不能为负数" in res2, f"{res2!r}")

    # 清理
    for sk in skill_service.list_skills():
        if sk.name.startswith("sandbox"):
            skill_service.soft_delete(sk)
print("  (sandbox 测试技能已清理)")

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：", FAILED)
    sys.exit(1)
