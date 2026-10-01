"""技能服务：AI 自写工具（Python 函数体）的受限编译、注册与 CRUD。

安全模型（尽力而为的护栏，非强安全边界 —— 依赖管理员人工启用）：
- code 只允许函数体；签名由 parameters.properties 推导
- 受限内置：无 __import__/open/eval/exec/compile/globals 等
- 静态扫描（正则黑名单 + AST 检查）：拒绝 import/子进程/网络/文件/反射逃逸
  （dunder 属性访问）/eval/exec/compile 调用与无 break 的 while 死循环
- 服务注入：白名单函数经闭包代理注入受限伪模块（SimpleNamespace 风格），
  不再注入整模块 —— 技能代码拿不到 db/Path/current_app 等宿主资源
- 子进程执行：Linux 下用 RLIMIT_AS/RLIMIT_CPU 限制内存与 CPU；代码长度 ≤ 64KB
- 新建默认 enabled=False，需管理员在「技能」页人工启用后才注册执行
"""
from __future__ import annotations

import ast
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

MAX_SKILL_CODE_LENGTH = 64 * 1024  # 技能代码最大长度（64KB），防超大 payload

# 受限内置：只给纯函数式/数据结构类能力
_SAFE_BUILTINS = {
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
    "format", "frozenset", "int", "isinstance", "len", "list", "map", "max",
    "min", "range", "repr", "reversed", "round", "set", "sorted", "str", "sum",
    "tuple", "zip", "Exception", "ValueError", "TypeError", "KeyError",
    "IndexError", "RuntimeError", "StopIteration",
}

# 静态扫描拒绝的特征（覆盖常见逃逸/副作用；死循环改用 AST 检查，见 _ast_check）
_FORBIDDEN = [
    r"\bimport\b", r"__import__", r"\beval\s*\(", r"\bexec\s*\(", r"\bcompile\s*\(",
    r"\bopen\s*\(", r"\bos\s*\.", r"\bsubprocess\b", r"\bsys\s*\.", r"\bsocket\b",
    r"\bimportlib\b", r"\bshutil\b", r"\bpathlib\b", r"\brequests\b", r"\burllib\b",
    r"__class__", r"__bases__", r"__subclasses__", r"__globals__", r"__mro__",
    r"__builtins__", r"__getattribute__", r"__code__", r"__dict__",
    r"\bgetattr\s*\(", r"\bsetattr\s*\(", r"\bdelattr\s*\(", r"\bglobals\s*\(",
    r"\blocals\s*\(", r"\bvars\s*\(", r"\binput\s*\(", r"\bbreakpoint\s*\(",
]

# AST 检查禁止的名字调用（对应正则之外的纵深防御）
_BANNED_CALL_NAMES = frozenset({
    "eval", "exec", "compile", "__import__", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "open", "input", "breakpoint", "memoryview",
})


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
            feature = pat.replace(r"\b", "")
            raise SkillError(f"技能代码包含被禁止的特征：{feature}")
    # 字符串常量拼接归一化后再扫一遍（防 "{0.__glo"+"bals__}" 切开黑名单子串）
    _scan_folded_strings(code)
    _scan_format_templates(code)


def _fold_string_binops(tree: ast.AST) -> ast.AST:
    """把常量字符串的 + 拼接折叠成单个 Constant，便于后续扫描。"""

    class _Folder(ast.NodeTransformer):
        def visit_BinOp(self, node):
            node = self.generic_visit(node)
            if isinstance(node.op, ast.Add):
                left, right = node.left, node.right
                if (isinstance(left, ast.Constant) and isinstance(right, ast.Constant)
                        and isinstance(left.value, str) and isinstance(right.value, str)):
                    return ast.copy_location(ast.Constant(value=left.value + right.value), node)
            return node

    return _Folder().visit(tree)


def _iter_str_constants(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node


def _const_str(node) -> str | None:
    """求值纯字符串常量表达式（Constant / 字符串 +）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _const_str(node.left), _const_str(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _scan_folded_strings(code: str) -> None:
    """对字符串常量做拼接归一化后再扫黑名单（捕获被切开的 dunder）。"""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        return
    tree = _fold_string_binops(tree)
    pieces = []
    for node in _iter_str_constants(tree):
        pieces.append((
            getattr(node, "lineno", 0) or 0,
            getattr(node, "col_offset", 0) or 0,
            node.value,
        ))
        for pat in _FORBIDDEN:
            if re.search(pat, node.value):
                feature = pat.replace(r"\b", "")
                raise SkillError(
                    f"技能代码字符串常量包含被禁止的特征：{feature}")
    # 按源码位置拼接全部字符串常量："{0.__glo"+"bals__}" 会拼回 __globals__
    joined = "".join(p[2] for p in sorted(pieces))
    if joined:
        for pat in _FORBIDDEN:
            if re.search(pat, joined):
                feature = pat.replace(r"\b", "")
                raise SkillError(
                    f"技能代码字符串常量拼接后包含被禁止的特征：{feature}")


def _check_format_template(template: str) -> None:
    """拒绝 format 字段里的 __ 属性或 [...] 下标（运行时会做 dunder 遍历）。"""
    from string import Formatter

    try:
        parts = list(Formatter().parse(template))
    except ValueError:
        return
    for _literal, field_name, format_spec, _conversion in parts:
        if field_name and ("__" in field_name or "[" in field_name):
            raise SkillError("技能代码禁止 format 模板中的属性或下标访问")
        if format_spec:
            _check_format_template(format_spec)


def _scan_format_templates(code: str) -> None:
    """对 .format() / format() 的模板串做词法检查。"""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        return
    tree = _fold_string_binops(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        templates: list[str] = []
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("format", "format_map"):
            tmpl = _const_str(func.value)
            if tmpl is not None:
                templates.append(tmpl)
        elif isinstance(func, ast.Name) and func.id == "format":
            if len(node.args) >= 2:
                tmpl = _const_str(node.args[1])
                if tmpl is not None:
                    templates.append(tmpl)
        for tmpl in templates:
            _check_format_template(tmpl)


# ---------- AST 静态检查（正则黑名单的纵深防御） ----------

def _is_const_true(node) -> bool:
    """while 测试是否为常量真值（True / 1）。"""
    if not isinstance(node, ast.Constant):
        return False
    return node.value is True or node.value == 1


def _loop_has_break(body) -> bool:
    """body 内是否存在直接归属本循环的 break（嵌套循环内的 break 不算）。"""
    for stmt in body:
        if isinstance(stmt, ast.Break):
            return True
        if isinstance(stmt, (ast.While, ast.For)):
            continue  # 嵌套循环：其 break 归内层
        if isinstance(stmt, ast.If):
            if _loop_has_break(stmt.body) or _loop_has_break(stmt.orelse):
                return True
        elif isinstance(stmt, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            if (_loop_has_break(stmt.body) or _loop_has_break(stmt.orelse)
                    or _loop_has_break(stmt.finalbody)
                    or any(_loop_has_break(h.body) for h in stmt.handlers)):
                return True
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            if _loop_has_break(stmt.body):
                return True
        elif isinstance(stmt, ast.Match):
            if any(_loop_has_break(case.body) for case in stmt.cases):
                return True
    return False


def _ast_check(code: str) -> None:
    """AST 级静态校验：import、dunder 属性访问、危险名字调用、无 break 的死循环。"""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        return  # 语法错误交给 compile 阶段报出（带行号，信息更友好）
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise SkillError("技能代码禁止 import 语句")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise SkillError(f"技能代码禁止访问双下划线属性：{node.attr}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in _BANNED_CALL_NAMES:
            raise SkillError(f"技能代码禁止调用：{node.func.id}")
    for node in ast.walk(tree):
        if isinstance(node, ast.While) and _is_const_true(node.test) \
                and not _loop_has_break(node.body):
            raise SkillError("技能代码禁止无 break 的 while 死循环")


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


def _build_service_namespaces() -> dict:
    """构建受限服务命名空间：白名单业务函数经闭包代理注入（不注入整模块）。

    每个服务对应一个受限伪模块（由 _skill_proxy.make_namespace 构建）：
    - 只含白名单业务函数的代理引用，模块对象本身不出现；
    - 代理函数定义在 _skill_proxy 模块，proxy.__globals__ 不含 db/Path/current_app，
      无法经 __globals__ 回溯宿主资源；
    - 非可调用常量（REPEAT_MAP 副本、ImageError 异常类）原样注入。
    """
    from app.services import (
        calendar_service, image_service, job_service, note_service, notify_service,
        page_service, task_service,
    )
    from app.services import _skill_proxy
    from app.utils.scoping import current_user_id

    owner_user_id = current_user_id()

    return {
        # 日历：事件 CRUD / 重复规则 / 提醒扫描（不含调度动作 scan_event_reminders）
        "calendar_service": _skill_proxy.make_namespace({
            "REPEAT_MAP": dict(calendar_service.REPEAT_MAP),  # 副本，防污染模块常量
            "rrule_to_repeat": calendar_service.rrule_to_repeat,
            "list_events": calendar_service.list_events,
            "get_event": calendar_service.get_event,
            "create_event": calendar_service.create_event,
            "update_event": calendar_service.update_event,
            "soft_delete_event": calendar_service.soft_delete_event,
            "upcoming_reminders": calendar_service.upcoming_reminders,
        }, owner_user_id),
        # 任务：CRUD / 状态 / 到期扫描（不含调度动作 scan_task_due）
        "task_service": _skill_proxy.make_namespace({
            "list_tasks": task_service.list_tasks,
            "create_task": task_service.create_task,
            "update_task": task_service.update_task,
            "set_task_status": task_service.set_task_status,
            "soft_delete_task": task_service.soft_delete_task,
            "due_tasks": task_service.due_tasks,
        }, owner_user_id),
        # 笔记：CRUD / 检索
        "note_service": _skill_proxy.make_namespace({
            "list_notes": note_service.list_notes,
            "create_note": note_service.create_note,
            "update_note": note_service.update_note,
            "soft_delete_note": note_service.soft_delete_note,
            "search_notes": note_service.search_notes,
        }, owner_user_id),
        # 定时任务：自定义提醒任务 CRUD（不含 reschedule / 调度动作）
        "job_service": _skill_proxy.make_namespace({
            "list_jobs": job_service.list_jobs,
            "create_reminder_job": job_service.create_reminder_job,
            "update_job": job_service.update_job,
            "delete_job": job_service.delete_job,
        }, owner_user_id),
        # 通知：发送 / 渠道查询（不含注册渠道、通知组写操作等管理功能）
        "notify_service": _skill_proxy.make_namespace({
            "notify": notify_service.notify,
            "notify_for": notify_service.notify_for,
            "available_channels": notify_service.available_channels,
            "default_channels": notify_service.default_channels,
            "list_notify_groups": notify_service.list_notify_groups,
            "expand_channels": notify_service.expand_channels,
        }, owner_user_id),
        # 图片：生成 / 改图 / 查询管理（不含 save_bytes 等文件写原语）
        "image_service": _skill_proxy.make_namespace({
            "ImageError": image_service.ImageError,
            "is_configured": image_service.is_configured,
            "generate": image_service.generate,
            "edit": image_service.edit,
            "get_image": image_service.get_image,
            "get_by_filename": image_service.get_by_filename,
            "list_images": image_service.list_images,
            "soft_delete": image_service.soft_delete,
            "set_public": image_service.set_public,
            "asset_url": image_service.asset_url,
        }, owner_user_id),
        # 网页：页面 CRUD / 公开访问解析
        "page_service": _skill_proxy.make_namespace({
            "default_template": page_service.default_template,
            "validate_slug": page_service.validate_slug,
            "generate_slug": page_service.generate_slug,
            "slug_exists": page_service.slug_exists,
            "get_page": page_service.get_page,
            "get_page_by_slug": page_service.get_page_by_slug,
            "create_page": page_service.create_page,
            "update_page": page_service.update_page,
            "soft_delete_page": page_service.soft_delete_page,
            "duplicate_page": page_service.duplicate_page,
            "list_pages": page_service.list_pages,
            "visible_by_slug": page_service.visible_by_slug,
            "page_port_configured": page_service.page_port_configured,
            "page_access_host": page_service.page_access_host,
            "page_site_base_url": page_service.page_site_base_url,
            "page_public_url": page_service.page_public_url,
            "page_domain_configured": page_service.page_domain_configured,
            "admin_domain_configured": page_service.admin_domain_configured,
        }, owner_user_id),
    }


def compile_skill_fn(name: str, parameters: dict, code: str) -> Callable:
    """把技能函数体编译为可调用函数（受限命名空间 + 白名单服务注入）。

    供主进程语法预检与子进程（skill_worker）执行共用。
    """
    if len(code) > MAX_SKILL_CODE_LENGTH:
        raise SkillError(
            f"技能代码过长（上限 {MAX_SKILL_CODE_LENGTH // 1024}KB，实际 {len(code)} 字符）")
    _scan_forbidden(code)
    _ast_check(code)
    params = validate_parameters(parameters)
    props = params.get("properties", {}) or {}
    required = set(params.get("required", []) or [])

    import keyword

    required_args = []
    optional_args = []
    for pname in props.keys():
        pname = str(pname)
        # 参数名会拼进函数签名：必须校验为合法标识符，防签名注入
        if not pname.isidentifier() or keyword.iskeyword(pname):
            raise SkillError(f"参数名非法（必须为合法 Python 标识符）：{pname}")
        if pname in required:
            required_args.append(pname)
        else:
            optional_args.append(f"{pname}=None")
    # JSON Schema 属性顺序不决定必填性，Python 签名必须先放必填参数。
    sig = ", ".join(required_args + optional_args)
    body = textwrap.indent(code.strip("\n"), "    ")
    src = f"def _skill_fn({sig}):\n{body}\n"
    _scan_forbidden(src)  # 签名拼接后再整体扫一遍（纵深防御）
    _ast_check(src)

    # 注入受限内置 + 常用模块 + 白名单服务（不注入 db：禁止任意 SQL；
    # 服务为受限伪模块，函数均经闭包代理包装，无法回溯宿主资源）
    import builtins
    import json
    from datetime import datetime

    from app.utils.timeutil import fmt_dt, parse_local, to_user, user_tz, utcnow as _utcnow

    ns = {
        "__builtins__": {k: getattr(builtins, k) for k in _SAFE_BUILTINS},
        "json": json, "datetime": datetime, "re": re,
        "utcnow": _utcnow, "fmt_dt": fmt_dt, "parse_local": parse_local,
        "to_user": to_user, "user_tz": user_tz,
        **_build_service_namespaces(),
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

    from app.utils.scoping import current_user_id

    worker = Path(__file__).resolve().parent.parent / "services" / "skill_worker.py"
    payload = _json.dumps({
        "name": skill.name, "parameters": skill.parameters,
        "code": skill.code, "args": args,
        "user_id": current_user_id(),
    }, ensure_ascii=False)
    try:
        proc = subprocess.run(
            [sys.executable, str(worker)], input=payload.encode("utf-8"),
            capture_output=True, timeout=SKILL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise SkillError(f"技能执行超时（>{SKILL_TIMEOUT}s），已强制终止") from None
    finally:
        # 子进程内业务服务可能已经提交任务增删改，即使技能随后失败或超时，
        # 主调度器也必须读取这些变更；无需额外的后台轮询。
        try:
            from app.services.job_service import reschedule

            reschedule()
        except Exception:  # noqa: BLE001 —— 保留技能本身的结果/异常
            logger.exception("技能执行后同步定时任务失败：%s", skill.name)
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
        "- 参数非法抛 ValueError(\"中文错误信息\")，模型会据此自我修正\n"
        "- 代码长度上限 64KB\n\n"
        "### 可用环境（受限沙箱，独立子进程执行）\n"
        "- 内置：len/str/int/float/bool/dict/list/tuple/set/range/sum/min/max/abs/round/"
        "sorted/enumerate/zip/map/filter/repr/format/isinstance/Exception/ValueError 等\n"
        "- 模块：json、datetime、re\n"
        "- 时间工具：utcnow()、fmt_dt(dt, tz)、parse_local(text, tz)、to_user(dt, tz)、user_tz(user)\n"
        "- 业务服务（只开放白名单函数，调用方式与从前一致）：calendar_service（事件）、"
        "task_service（任务）、note_service（笔记）、job_service（定时任务）、"
        "notify_service（通知）、image_service（图片）、page_service（网页）\n\n"
        "### 禁止\n"
        "import / 文件读写 / 网络请求 / 子进程 / 反射逃逸（__class__、__globals__ 等 "
        "双下划线属性）/ 无 break 的 while 死循环 / eval、exec、compile / "
        "直接操作数据库（db 不可用，数据操作请用业务服务）\n\n"
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
    if len(code) > MAX_SKILL_CODE_LENGTH:
        raise SkillError(f"技能代码过长（上限 {MAX_SKILL_CODE_LENGTH // 1024}KB）")
    _scan_forbidden(code)
    _ast_check(code)
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
    """更新技能；改 code/parameters 时先编译校验。

    已启用技能若改了 code/parameters，必须停用并注销，待管理员重新审核启用。
    """
    touch_impl = parameters is not None or code is not None
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
        if len(code) > MAX_SKILL_CODE_LENGTH:
            raise SkillError(f"技能代码过长（上限 {MAX_SKILL_CODE_LENGTH // 1024}KB）")
        _scan_forbidden(code)
        _ast_check(code)
        skill.code = code
    # 编译校验（用最新字段构造临时对象）
    tmp = Skill(name=skill.name, description=skill.description,
                parameters=skill.parameters, code=skill.code, enabled=skill.enabled)
    _compile_skill(tmp)
    if skill.enabled and touch_impl:
        _unregister(skill.name)
        skill.enabled = False
    elif skill.enabled:
        _unregister(skill.name)
        _register(skill)
    db.session.commit()
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
