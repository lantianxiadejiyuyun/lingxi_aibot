"""技能（自进化）测试：沙箱拒绝、创建默认禁用、启用注册执行、命名冲突、HTTP 页面与启停删除。"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import test_common

BASE = "http://127.0.0.1:5000"
PASSED, FAILED = [], []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(name)
        print(f"  ✗ {name} {extra}")


# ---- HTTP 页面 ----
s = requests.Session()
token = test_common.login(s, BASE)
check("登录成功", bool(token))
check("技能页 200", s.get(BASE + "/skills").status_code == 200)
check("记忆页 200", s.get(BASE + "/memory").status_code == 200)
check("导航含技能/记忆入口", "🧩" in s.get(BASE + "/").text and "🧠" in s.get(BASE + "/").text)

# ---- 进程内：沙箱与注册执行 ----
from run import app
from app.services import skill_service
from app.ai import registry

with app.app_context():
    # 1. 沙箱拒绝危险代码
    rejected = False
    try:
        skill_service.create_skill("bad_skill", "危险技能",
                                   {"type": "object", "properties": {}, "required": []},
                                   "import os\nos.system('echo hi')\nreturn 'x'")
    except skill_service.SkillError:
        rejected = True
    check("沙箱拒绝 import os", rejected)

    # 2. 命名冲突（与内置工具重名）
    conflict = False
    try:
        skill_service.create_skill("list_tasks", "冲突",
                                   {"type": "object", "properties": {}, "required": []},
                                   "return 'x'")
    except skill_service.SkillError:
        conflict = True
    check("拒绝与内置工具重名", conflict)

    # 3. 良性技能：创建默认禁用 → 启用 → 注册执行
    skill = skill_service.create_skill(
        "test_echo", "测试技能：原样返回前缀 echo",
        {"type": "object", "properties": {"msg": {"type": "string"}},
         "required": ["msg"]},
        "return 'echo:' + msg")
    check("创建默认禁用", skill.enabled is False)
    check("禁用态未注册", registry.get_tool("test_echo") is None)
    skill_service.set_enabled(skill, True)
    check("启用后已注册", registry.get_tool("test_echo") is not None)
    result = registry.execute_tool("test_echo", {"msg": "hi"})
    check("技能可执行且结果正确", result == "echo:hi", f"result={result!r}")
    skill_service.set_enabled(skill, False)
    check("停用后已注销", registry.get_tool("test_echo") is None)

    # 4. HTTP 启停/删除（服务器进程内注册）
    http_id = skill.id
    skill_service.set_enabled(skill, False)  # 确保初始禁用
    # 重新启用（进程内），但用 HTTP 验证服务器侧状态
    skill_service.set_enabled(skill, True)

r = s.post(BASE + "/skills/api/toggle", json={"skill_id": http_id, "enabled": False})
check("HTTP 停用", r.json().get("ok") is True, str(r.json()))
r = s.post(BASE + "/skills/api/toggle", json={"skill_id": http_id, "enabled": True})
check("HTTP 启用", r.json().get("ok") is True and r.json()["data"]["enabled"] is True, str(r.json()))
r = s.get(BASE + "/skills")
check("技能页显示 test_echo", "test_echo" in r.text)
r = s.post(BASE + "/skills/api/delete", json={"skill_id": http_id})
check("HTTP 删除", r.json().get("ok") is True, str(r.json()))
r = s.get(BASE + "/skills")
check("删除后列表不含", "test_echo" not in r.text)

print(f"\n结果：通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("失败项：", FAILED)
    sys.exit(1)
