"""技能沙箱代理工厂（内部模块，业务代码勿直接使用）。

把白名单业务函数包装为「闭包代理」后再注入技能命名空间，用于切断反射逃逸：
- 代理函数（_proxy）定义在本模块，因此 proxy.__globals__ 只包含本模块的全局名
  （make_proxy / make_namespace 与 __builtins__ 等），不含 db、Path、current_app、
  sqlalchemy 等宿主资源 —— 技能代码即使拿到代理对象，也无法经 __globals__ 回溯到
  原业务模块；
- 原函数 fn 仅作为 _proxy 的闭包单元变量（cell）存在，访问它需要 __closure__ 等
  双下划线属性（已被技能静态校验禁止）；
- 伪模块用动态空类实例承载，不引入 types 等模块对象，进一步压缩 __globals__ 内容。
"""
from __future__ import annotations

import inspect

__all__ = ["make_proxy", "make_namespace"]


def make_proxy(fn, owner_user_id=None):
    """把函数包装为闭包代理：调用行为不变，但不暴露 fn 本身与 fn.__globals__。

    owner_user_id 非空时，若原函数签名含 user_id，则无论技能传入位置参数还是
    关键字参数，一律覆写为调用者 id（技能不能指定他人 user_id）。
    """
    if owner_user_id is None:
        def _proxy(*args, **kwargs):
            return fn(*args, **kwargs)
        return _proxy

    try:
        param_names = list(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        param_names = []
    uid_idx = param_names.index("user_id") if "user_id" in param_names else -1

    def _proxy(*args, **kwargs):
        if uid_idx >= 0:
            kwargs.pop("user_id", None)
            if uid_idx < len(args):
                args_list = list(args)
                args_list[uid_idx] = owner_user_id
                args = tuple(args_list)
            else:
                kwargs["user_id"] = owner_user_id
        return fn(*args, **kwargs)
    return _proxy


def make_namespace(funcs, owner_user_id=None):
    """按 {属性名: 值} 构建受限伪模块（仅含白名单内容）。

    - 普通函数：经 make_proxy 包装成闭包代理（不暴露原函数与 __globals__，
      并强制覆写 user_id 为调用者）；
    - 类（如异常类 ImageError）：原样注入 —— 类对象不暴露模块（回溯模块需
      __module__/__mro__ 等双下划线属性，已被技能静态校验禁止），且必须保持类
      语义以支持 `except image_service.ImageError` / isinstance 用法；
    - 其他非可调用值（字符串/副本常量）：原样注入。
    返回实例无模块属性（__name__/__file__ 等），也无业务模块引用。
    """
    ns = type("SandboxNamespace", (object,), {})()
    for key, value in funcs.items():
        if isinstance(value, type):
            setattr(ns, key, value)
        else:
            setattr(ns, key, make_proxy(value, owner_user_id) if callable(value) else value)
    return ns
