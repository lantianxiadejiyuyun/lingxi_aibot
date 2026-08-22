"""技能服务：AI 自写工具（Python 函数体）的受限编译、注册与 CRUD。

安全模型（尽力而为的护栏，非强安全边界 —— 依赖单用户信任 + 人工启用）：
- code 只允许函数体；签名由 parameters.properties 推导
- 受限内置：无 __import__/open/eval/exec/compile/globals 等
- 静态扫描：拒绝 import/子进程/网络/文件/反射逃逸特征与 while True 死循环
- 新建默认 enabled=False，需在「技能」页人工启用后才注册执行
"""
from __future__ import annotations

import logging
import re
import textwrap
from typing import Callable, Optional

from app.extensions import db
from app.models.skill import Skill
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

SKILL_TIMEOUT = 10  # 技能子进程执行超时（秒），超时强杀

# 受限内置：只给纯函数式/数据结构类能力
_SAFE_BUILTINS = {
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
    "format", "frozenset", "int", "isinstance", "len", "list", "map", "max",
    "min", "range", "repr", "reversed", "round", "set", "sorted", "str", "sum",
    "tuple", "zip", "Exception", "ValueError", "TypeError", "KeyError",
    "IndexError", "RuntimeError", "StopIteration",
}

# 静态扫描拒绝的特征（覆盖常见逃逸/副作用/死循环）
_FORBIDDEN = [
    r"\bimport\b", r"__import__", r"\beval\s*\(", r"\bexec\s*\(", r"\bcompile\s*\(",
    r"\bopen\s*\(", r"\bos\s*\.", r"\bsubprocess\b", r"\bsys\s*\.", r"\bsocket\b",
    r"\bimportlib\b", r"\bshutil\b", r"\bpathlib\b", r"\brequests\b", r"\burllib\b",
    r"__class__", r"__bases__", r"__subclasses__", r"__globals__", r"__mro__",
    r"__builtins__", r"__getattribute__", r"__code__", r"__dict__",
    r"\bgetattr\s*\(", r"\bsetattr\s*\(", r"\bdelattr\s*\(", r"\bglobals\s*\(",
    r"\blocals\s*\(", r"\bvars\s*\(", r"\binput\s*\(", r"\bbreakpoint\s*\(",
    r"\bwhile\s+True\b", r"\bwhile\s+1\b",
]


class SkillError(Exception):
    """技能创建/编译/启用失败（信息回传模型或前端）。"""


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise SkillError("技能名需为小写字母开头，2-64 位，仅含字母/数字/下划线")
    return name


def _scan_forbidden(code: str) -> None:
    for pat in _FORBIDDEN:
        if re.search(pat, code):
            raise SkillError(f"技能代码包含被禁止的特征：{pat.strip('\\\\b')}")


def validate_parameters(parameters) -> dict:
    """校验 JSON Schema：需含 type=object 与 properties。"""
    if not isinstance(parameters, dict):
        raise SkillError("parameters 必须是 JSON Schema 对象")
    if parameters.get("type") != "object":
        raise SkillError("parameters.type 必须为 object")
    props = parameters.get("properties")
    if not isinstance(props, dict):
        raise SkillError("parameters 需包含 properties 字典")
    return parameters


def compile_skill_fn(name: str, parameters: dict, code: str) -> Callable:
    """把技能函数体编译为可调用函数（受限命名空间 + 注入服务）。

    供主进程语法预检与子进程（skill_worker）执行共用。
    """
    _scan_forbidden(code)
    params = validate_parameters(parameters)
    props = params.get("properties", {}) or {}
    required = set(params.get("required", []) or [])

    args = []
    for pname in props.keys():
        if pname in required:
            args.append(pname)
        else:
            args.append(f"{pname}=None")
    sig = ", ".join(args)
    body = textwrap.indent(code.strip("\n"), "    ")
    src = f"def _skill_fn({sig}):\n{body}\n"

    # 注入受限内置 + 常用模块 + 业务服务
    import builtins
    import json
    from datetime import datetime

    from app.services import (
        calendar_service, image_service, job_service, note_service, notify_service,
        page_service, task_service,
    )
    from app.utils.timeutil import fmt_dt, parse_local, to_user, user_tz, utcnow as _utcnow

    ns = {
        "__builtins__": {k: getattr(builtins, k) for k in _SAFE_BUILTINS},
        "db": db, "json": json, "datetime": datetime, "re": re,
        "utcnow": _utcnow, "fmt_dt": fmt_dt, "parse_local": parse_local,
        "to_user": to_user, "user_tz": user_tz,
        "calendar_service": calendar_service, "task_service": task_service,
        "note_service": note_service, "job_service": job_service,
        "notify_service": notify_service, "image_service": image_service,
        "page_service": page_service,
    }
    try:
        exec(compile(src, f"<skill:{name}>", "exec"), ns)
    except SyntaxError as e:
        raise SkillError(f"技能代码语法错误：{e}") from e
    return ns["_skill_fn"]


def _compile_skill(skill: Skill) -> Callable:
    """把技能对象编译为可调用函数。"""
    return compile_skill_fn(skill.name, skill.parameters, skill.code)


def _run_in_subprocess(skill: Skill, args: dict):
    """在子进程中受限执行技能（超时强杀，防止死循环挂死主进程）。"""
    import json as _json
    import subprocess
    import sys
    from pathlib import Path

    worker = Path(__file__).resolve().parent.parent / "services" / "skill_worker.py"
    payload = _json.dumps({
        "name": skill.name, "parameters": skill.parameters,
        "code": skill.code, "args": args,
    }, ensure_ascii=False)
    try:
        proc = subprocess.run(
            [sys.executable, str(worker)], input=payload.encode("utf-8"),
            capture_output=True, timeout=SKILL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise SkillError(f"技能执行超时（>{SKILL_TIMEOUT}s），已强制终止") from None
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")[:200]
        raise SkillError(f"技能执行失败（exit {proc.returncode}）：{err}")
    try:
        result = _json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError:
        out = (proc.stdout or b"").decode("utf-8", "replace")[:200]
        raise SkillError(f"技能输出解析失败：{out}") from None
    if isinstance(result, dict) and result.get("error"):
        err = str(result["error"])
        if err.startswith("ValueError:"):
            raise ValueError(err[len("ValueError:"):].strip())
        raise SkillError(err[:300])
    return result.get("result")


def _register(skill: Skill) -> Callable:
    """编译并注册技能到 AI 工具注册表（执行走子进程，超时强杀）。"""
    from app.ai.registry import register_tool

    compile_skill_fn(skill.name, skill.parameters, skill.code)  # 语法/扫描预检
    wrapper = lambda **kwargs: _run_in_subprocess(skill, kwargs)  # noqa: E731
    register_tool(skill.name, skill.description, skill.parameters, dangerous=True)(wrapper)
    return wrapper


def _unregister(name: str) -> None:
    """从工具注册表移除技能（registry 提供删除入口）。"""
    from app.ai import registry

    registry.remove_tool(name)


# ---------- CRUD ----------

def get_skill(skill_id) -> Optional[Skill]:
    try:
        skill_id = int(skill_id)
    except (TypeError, ValueError):
        return None
    skill = db.session.get(Skill, skill_id)
    if skill is None or skill.deleted_at is not None:
        return None
    return skill


def get_skill_by_name(name: str) -> Optional[Skill]:
    return Skill.query.filter(Skill.name == name, Skill.deleted_at.is_(None)).first()


def list_skills() -> list[Skill]:
    return (Skill.query.filter(Skill.deleted_at.is_(None))
            .order_by(Skill.created_at.desc()).all())


def skill_template() -> str:
    """技能编写规范（供 AI 生成技能时参考）。"""
    return (
        "## 技能编写规范（灵犀）\n\n"
        "### 参数签名推导\n"
        "parameters 需为 JSON Schema：{\"type\": \"object\", \"properties\": {键: 描述}, "
        "\"required\": [必填键]}。properties 的每个键即函数参数名：\n"
        "- required 中的参数：无默认值，模型必须提供\n"
        "- 其余参数：自动加默认值 None，可省略\n\n"
        "### code（函数体，不含 def 行）\n"
        "- 直接用参数名写逻辑，返回 str 或可 JSON 序列化的对象（dict/list/数字）\n"
        "- 参数非法抛 ValueError(\"中文错误信息\")，模型会据此自我修正\n\n"
        "### 可用环境（受限沙箱）\n"
        "- 内置：len/str/int/float/bool/dict/list/tuple/set/range/sum/min/max/abs/round/"
        "sorted/enumerate/zip/map/filter/repr/format/isinstance/Exception/ValueError 等\n"
        "- 模块：db、json、datetime、re\n"
        "- 时间工具：utcnow()、fmt_dt(dt, tz)、parse_local(text, tz)、to_user(dt, tz)、user_tz(user)\n"
        "- 业务服务：calendar_service（事件）、task_service（任务）、note_service（笔记）、"
        "job_service（定时任务）、notify_service（通知）、image_service（图片）、"
        "page_service（网页）\n\n"
        "### 禁止\n"
        "import / 文件读写 / 网络请求 / 子进程 / 反射逃逸（__class__ 等）/ while True 死循环\n\n"
        "### 完整示例\n"
        "name: title_case\n"
        "description: 把输入文本的每个单词首字母大写（用于标题美化）\n"
        "parameters: {\"type\": \"object\", \"properties\": {\"text\": {\"type\": \"string\"}}, "
        "\"required\": [\"text\"]}\n"
        "code:\n"
        "    if not text or not text.strip():\n"
        "        raise ValueError(\"text 不能为空\")\n"
        "    return text.strip().title()\n"
    )


def create_skill(name: str, description: str, parameters: dict, code: str) -> Skill:
    """创建技能（默认禁用，需人工启用）。"""
    name = validate_name(name)
    if get_skill_by_name(name) is not None:
        raise SkillError(f"技能名已存在：{name}")
    from app.ai.registry import get_tool

    if get_tool(name) is not None:
        raise SkillError(f"技能名与内置工具冲突：{name}")
    description = (description or "").strip()
    if not description:
        raise SkillError("技能描述不能为空（模型依赖它决定何时调用）")
    validate_parameters(parameters)
    code = (code or "").strip()
    if not code:
        raise SkillError("技能代码不能为空")
    _scan_forbidden(code)
    # 语法预检（不注册）
    tmp = Skill(name=name, description=description, parameters=parameters, code=code, enabled=False)
    _compile_skill(tmp)

    skill = Skill(name=name, description=description, parameters=parameters,
                  code=code, enabled=False)
    db.session.add(skill)
    db.session.commit()
    return skill


def update_skill(skill: Skill, description: Optional[str] = None,
                 parameters: Optional[dict] = None, code: Optional[str] = None) -> Skill:
    """更新技能；改 code/parameters 时先编译校验，禁用态下更新后重新注册。"""
    if description is not None:
        description = (description or "").strip()
        if not description:
            raise SkillError("技能描述不能为空")
        skill.description = description
    if parameters is not None:
        validate_parameters(parameters)
        skill.parameters = parameters
    if code is not None:
        code = (code or "").strip()
        if not code:
            raise SkillError("技能代码不能为空")
        _scan_forbidden(code)
        skill.code = code
    # 编译校验（用最新字段构造临时对象）
    tmp = Skill(name=skill.name, description=skill.description,
                parameters=skill.parameters, code=skill.code, enabled=skill.enabled)
    _compile_skill(tmp)
    db.session.commit()
    if skill.enabled:
        _unregister(skill.name)
        _register(skill)
    return skill


def set_enabled(skill: Skill, enabled: bool) -> Skill:
    """启用/禁用。启用即编译并注册（失败不落库）。"""
    if enabled and not skill.enabled:
        _register(skill)  # 编译失败抛 SkillError，不改变状态
    elif not enabled and skill.enabled:
        _unregister(skill.name)
    skill.enabled = bool(enabled)
    db.session.commit()
    return skill


def soft_delete(skill: Skill) -> None:
    """软删除；若已启用先注销，并改写名字（<原>_d<id>）释放原名避免唯一索引冲突。"""
    if skill.enabled:
        _unregister(skill.name)
    skill.enabled = False
    skill.deleted_at = utcnow()
    skill.name = f"{skill.name[:50]}_d{skill.id}"
    db.session.commit()


def load_skills() -> list[str]:
    """启动时加载全部启用技能到工具注册表（单个失败跳过不阻断启动；表未建时静默）。"""
    loaded = []
    try:
        rows = (Skill.query.filter(Skill.enabled.is_(True), Skill.deleted_at.is_(None)).all())
    except Exception:  # noqa: BLE001 —— 首次 init-db 前 skills 表尚未创建
        return loaded
    for skill in rows:
        try:
            _register(skill)
            loaded.append(skill.name)
        except SkillError as e:
            logger.warning("技能 %s 加载失败：%s", skill.name, e)
    return loaded
