"""Trace class flags copied into slots and consumed by pure string transformations."""
import ast
import builtins
from contextvars import ContextVar
from functools import lru_cache
import inspect
from pathlib import Path
import sys
import types

import profile_class_reads as meta
import profile_semantics as base


fields = set()
settings = set()
muted = ContextVar("muted_copy", default=None)


def operation(node, argument):
    if isinstance(node, ast.Name) and node.id == argument:
        return "identity"
    if (isinstance(node, ast.Call) and not node.args and not node.keywords
        and isinstance(node.func, ast.Attribute) and node.func.attr in ("upper", "lower", "casefold")
        and isinstance(node.func.value, ast.Name) and node.func.value.id == argument):
        return node.func.attr


def accepts(cond, value, owner, names):
    if isinstance(cond, ast.BoolOp):
        parts = [accepts(n, value, owner, names) for n in cond.values]
        if None not in parts:
            return (any if isinstance(cond.op, ast.Or) else all)(parts)
    if (isinstance(cond, ast.Compare) and len(cond.ops) == 1 and isinstance(cond.left, ast.Attribute)
        and isinstance(cond.left.value, ast.Name) and cond.left.value.id == owner
        and isinstance(cond.comparators[0], ast.Constant)):
        names.add(cond.left.attr)
        right = cond.comparators[0].value
        if isinstance(cond.ops[0], ast.Eq):
            return value == right
        if isinstance(cond.ops[0], ast.Is) and (right is None or type(right) is bool):
            return value is right


def consumer(fn):
    if (not isinstance(fn, ast.FunctionDef) or fn.decorator_list or len(fn.args.args) != 2
        or fn.args.vararg or fn.args.kwarg or fn.args.kwonlyargs or fn.args.posonlyargs):
        return None
    owner, arg = [p.arg for p in fn.args.args]
    names = set()
    choices = {}
    if not isinstance(fn.body[-1], ast.Return):
        return None
    fallback = operation(fn.body[-1].value, arg)
    if not fallback:
        return None
    for branch in fn.body[:-1]:
        if not (isinstance(branch, ast.If) and not branch.orelse and len(branch.body) == 1
                and isinstance(branch.body[0], ast.Return)):
            return None
        action = operation(branch.body[0].value, arg)
        if not action:
            return None
        for v in (False, True):
            matched = accepts(branch.test, v, owner, names)
            if matched is None:
                return None
            if matched:
                choices.setdefault(v, action)
    if len(names) == 1:
        return names.pop(), arg, choices.get(False, fallback), choices.get(True, fallback)


class Slot:
    def __init__(self, raw, name):
        self.raw, self.name = raw, name
        self.site = base.profile.site("copy-consumer:" + name)

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        if muted.get() is not self:
            base.profile.hit(self.site, "possible")
        return self.raw.__get__(obj, owner)

    def __set__(self, obj, value):
        self.raw.__set__(obj, value)

    def __delete__(self, obj):
        self.raw.__delete__(obj)


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
        if excluded is None:
            return cls
        for name in (set(names) & settings) - excluded:
            value = vars(cls).get(name)
            if type(value) is bool:
                site = base.profile.site(f"copy:{cls.__module__}.{cls.__qualname__}:{name}")
                setattr(cls, name, Flag(site, value))
        return cls
    return decorate


def slots(cls):
    if (type(cls) is type and cls.__getattribute__ is object.__getattribute__
        and cls.__setattr__ is object.__setattr__):
        for name in fields:
            raw = vars(cls).get(name)
            if type(raw) is types.MemberDescriptorType:
                setattr(cls, name, Slot(raw, name))
    return cls


@lru_cache(None)
def slot_for(cls, name):
    if cls.__getattribute__ is object.__getattribute__ and cls.__setattr__ is object.__setattr__:
        return inspect.getattr_static(cls, name, None)


def direct(obj, key, desc):
    token = muted.set(desc)
    try:
        return getattr(obj, key)
    finally:
        muted.reset(token)


def assign(source, key, receiver, field):
    desc = inspect.getattr_static(type(source) if not isinstance(source, type) else source, key, None)
    if isinstance(desc, Flag) and isinstance(slot_for(type(receiver), field), Slot):
        base.profile.hit(desc.site, "writes:" + field)
        return direct(source, key, desc)
    return getattr(source, key)


def value(obj, key, arg, old, new):
    slot = slot_for(type(obj), key)
    if type(arg) is str and isinstance(slot, Slot):
        first = arg if old == "identity" else getattr(str, old)(arg)
        second = arg if new == "identity" else getattr(str, new)(arg)
        if first == second:
            return direct(obj, key, slot)
    return getattr(obj, key)


def sources(stmt):
    if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Attribute)
            and stmt.targets[0].attr in fields and isinstance(stmt.targets[0].value, ast.Name)):
        return []
    expr = stmt.value
    return [n for n in ([expr.body, expr.orelse] if isinstance(expr, ast.IfExp) else [expr])
            if isinstance(n, ast.Attribute) and n.attr.isupper()]


class Transform(meta.Transform):
    def visit_ClassDef(self, node):
        plain = not node.decorator_list
        super().visit_ClassDef(node)
        if plain and self.path.startswith("sqlglot/"):
            node.decorator_list.insert(0, ast.copy_location(ast.Name("_rwx_copy_slots", ast.Load()), node))
        return node

    def visit_FunctionDef(self, node):
        spec = consumer(node)
        if spec:
            field, arg, old, new = spec
            for branch in node.body[:-1]:
                for parent in ast.walk(branch.test):
                    if isinstance(parent, ast.Compare):
                        attr = parent.left
                        parent.left = self.wrap(attr, "_rwx_copy_value", [attr.value, ast.Constant(field),
                                                ast.Name(arg, ast.Load()), ast.Constant(old), ast.Constant(new)])
        return self.generic_visit(node)

    def visit_Assign(self, node):
        for source in sources(node):
            target = node.targets[0]
            new = self.wrap(source, "_rwx_copy_assign", [source.value, ast.Constant(source.attr),
                            ast.Name(target.value.id, ast.Load()), ast.Constant(target.attr)])
            if node.value is source:
                node.value = new
            elif node.value.body is source:
                node.value.body = new
            else:
                node.value.orelse = new
        return self.generic_visit(node)


if __name__ == "__main__":
    trees = [ast.parse(p.read_bytes()) for p in (Path(sys.argv[1])/"sqlglot").rglob("*.py")]
    for tree in trees:
        for node in ast.walk(tree):
            if plan := consumer(node):
                fields.add(plan[0])
    for tree in trees:
        for node in ast.walk(tree):
            settings.update(source.attr for source in sources(node))
    del trees
    builtins._rwx_copy_slots = slots
    builtins._rwx_copy_value = value
    builtins._rwx_copy_assign = assign
    builtins._rwx_scalars = install
    builtins._rwx_reflected = lambda obj: meta.namespace(obj, "all")
    builtins._rwx_ns = meta.namespace
    base.Transform = Transform
    base.Profile.entry = lambda self, code, offset: sys.monitoring.DISABLE
    base.Profile.audit = lambda self, event, args: None
    base.main()
