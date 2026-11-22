"""Identify class flag reads whose next short-circuit operand fixes the result."""
import ast
import builtins
from contextvars import ContextVar
import enum
import inspect
import sys

import profile_branches as branch
import profile_class_reads as meta
import profile_semantics as base


muted = ContextVar("neutral_flag", default=None)


class Flag:
    def __init__(self, site, value):
        self.site, self.value = site, value

    def __get__(self, obj, owner=None):
        if muted.get() is not self:
            base.profile.hit(self.site, "read")
        return self.value


def install(names):
    def decorate(cls):
        if (not cls.__module__.startswith("sqlglot.") or cls.__getattribute__ is not object.__getattribute__
            or any("__init_subclass__" in vars(c) for c in cls.__mro__ if c is not object)):
            return cls
        excluded = set() if type(cls) is type else meta.reserved(type(cls))
        if excluded is not None:
            for name in set(names) - excluded:
                val = vars(cls).get(name)
                if type(val) is bool:
                    site = base.profile.site(f"switch:{cls.__module__}.{cls.__qualname__}:{name}")
                    setattr(cls, name, Flag(site,val))
        return cls
    return decorate


def native(v):
    return type(v) in branch.SCALARS or (isinstance(v,enum.IntEnum) and type(v).__eq__ is int.__eq__
                                          and type(v).__ne__ is int.__ne__)


def evaluate(plan, frame):
    op, *args = plan
    if op == "name":
        name = args[0]
        if name in frame.f_locals:
            return frame.f_locals[name]
        code = frame.f_code
        if name in code.co_varnames or name in code.co_cellvars or name in code.co_freevars:
            return branch.UNKNOWN
        return frame.f_globals.get(name,branch.UNKNOWN)
    if op == "literal":
        return args[0]
    x = evaluate(args[0],frame)
    if x is branch.UNKNOWN:
        return branch.UNKNOWN
    if op == "attr":
        if isinstance(x,type):
            if type(x).__getattribute__ is type.__getattribute__:
                v = vars(x).get(args[1],branch.UNKNOWN)
                if not hasattr(type(v), "__get__"):
                    return v
            return branch.UNKNOWN
        return branch.stored(x,args[1])
    if op == "get":
        return x.get(args[1]) if type(x) is dict and all(type(k) is str for k in x) else branch.UNKNOWN
    if op == "not":
        v = branch.safe_truth(x)
        return branch.UNKNOWN if v is branch.UNKNOWN else not v
    y = evaluate(args[1],frame)
    if y is not branch.UNKNOWN:
        if op in ("Is","IsNot"):
            return (x is y) == (op == "Is")
        if native(x) and native(y):
            return (x == y) == (op == "Eq")
    return branch.UNKNOWN


def observe(obj, name, plan, fixed, condition):
    cls = obj if isinstance(obj,type) else type(obj)
    flag = inspect.getattr_static(cls,name,None)
    if isinstance(flag,Flag):
        base.profile.hit(flag.site,"analyzed")
        # A passing observation cannot cancel a different use in the same test.
        if (flag.site,"read") not in base.profile.seen:
            v = evaluate(plan,sys._getframe(1))
            if condition:
                v = branch.safe_truth(v)
            if v is fixed:
                token = muted.set(flag)
                try:
                    return getattr(obj,name)
                finally:
                    muted.reset(token)
    return getattr(obj,name)


class Transform(meta.Transform):
    def visit_Module(self, node):
        conditions = {n.test for n in ast.walk(node) if isinstance(n,(ast.If,ast.While,ast.IfExp))}
        for expr in ast.walk(node):
            if not isinstance(expr,ast.BoolOp) or len(expr.values)<2:
                continue
            test, next_ = expr.values[:2]
            ref = test.operand if isinstance(test,ast.UnaryOp) and isinstance(test.op,ast.Not) else test
            if not isinstance(ref,ast.Attribute) or not ref.attr.isupper():
                continue
            plan = branch.encode(next_)
            if plan:
                replacement = self.wrap(ref,"_rwx_switch",[ref.value,ast.Constant(ref.attr),ast.Constant(plan),
                                        ast.Constant(isinstance(expr.op,ast.Or)),ast.Constant(expr in conditions)])
                if ref is test:
                    expr.values[0] = replacement
                else:
                    test.operand = replacement
        return self.generic_visit(node)


if __name__ == "__main__":
    builtins._rwx_switch = observe
    builtins._rwx_scalars = install
    builtins._rwx_reflected = lambda obj: meta.namespace(obj,"all")
    builtins._rwx_ns = meta.namespace
    base.Transform = Transform
    base.Profile.entry = lambda self, code, offset: sys.monitoring.DISABLE
    base.Profile.audit = lambda self, event, args: None
    base.main()
