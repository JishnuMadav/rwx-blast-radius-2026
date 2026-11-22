"""Trace observable reads of Boolean settings guarding loops and private scratch lists."""
import ast
import builtins
from functools import lru_cache
import inspect
import sys
import textwrap
from contextvars import ContextVar

import profile_class_reads as meta
import profile_semantics as base


def loop_shape(node):
    if (not isinstance(node, ast.If) or not isinstance(node.test, ast.Attribute)
        or not node.test.attr.isupper() or node.orelse or len(node.body) != 1):
        return None
    loop = node.body[0]
    if (not isinstance(loop, ast.For) or loop.orelse or len(loop.body) != 1
        or not isinstance(loop.target, ast.Name)):
        return None
    guard = loop.body[0]
    if not (isinstance(guard, ast.If) and not guard.orelse and isinstance(guard.test, ast.Call)):
        return None
    check = guard.test
    if not (isinstance(check.func, ast.Name) and check.func.id == "isinstance"
            and len(check.args) == 2 and not check.keywords and isinstance(check.args[0], ast.Name)
            and check.args[0].id == loop.target.id and isinstance(loop.iter, ast.Attribute)
            and isinstance(loop.iter.value, ast.Name) and isinstance(check.args[1], (ast.Attribute, ast.Name))):
        return None
    return loop.iter, check.args[1], guard


def private_append(node, owner, parents, functions, loops):
    if not (isinstance(node, ast.If) and isinstance(node.test, ast.Attribute) and not node.orelse
            and len(node.body) == 1 and isinstance(node.body[0], ast.Expr)):
        return None
    call = node.body[0].value
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "append"
            and isinstance(call.func.value, ast.Name) and len(call.args) == 1
            and isinstance(call.args[0], ast.Name) and not call.keywords):
        return None
    name = call.func.value.id
    writes = 0
    uses = []
    for ref in ast.walk(owner):
        if not isinstance(ref, ast.Name) or ref.id != name or ref is call.func.value:
            continue
        stmt = parents[ref]
        if isinstance(ref.ctx, ast.Store):
            if (not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or not isinstance(stmt.value, ast.List)
                or stmt.value.elts or (isinstance(stmt, ast.Assign) and len(stmt.targets) != 1)):
                return None
            writes += 1
        elif isinstance(stmt, ast.Call) and isinstance(stmt.func, ast.Name) and ref in stmt.args:
            callee = functions.get(stmt.func.id)
            pos = stmt.args.index(ref)
            if callee is None or callee.decorator_list or pos >= len(callee.args.args):
                return None
            param = callee.args.args[pos].arg
            allowed = set()
            for guard, (_, _, body) in loops.items():
                if guard.test.attr == node.test.attr:
                    allowed.update(ast.walk(body))
            refs = [n for n in ast.walk(callee) if isinstance(n, ast.Name) and n.id == param]
            if not refs or any(n not in allowed for n in refs):
                return None
            uses.append((stmt, pos, callee))
        else:
            return None
    return uses if writes == 1 and uses else None


def inspects_locals(fn):
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and (n.func.id in ("locals", "eval", "exec") or (n.func.id == "vars" and not n.args))
               for n in ast.walk(fn))


def isolated_loop(guard, parents):
    fn = guard
    while fn in parents and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        fn = parents[fn]
    if not isinstance(fn, ast.FunctionDef) or inspects_locals(fn):
        return False
    loop = guard.body[0]
    inside = set(ast.walk(loop))
    return all(n in inside for n in ast.walk(fn)
               if isinstance(n, ast.Name) and n.id == loop.target.id)


class Setting:
    def __init__(self, site, value):
        self.site, self.value = site, value

    def __get__(self, obj, owner=None):
        if self is not muted.get():
            base.profile.hit(self.site, "read")
        return self.value


def install(names):
    def decorate(cls):
        if (not cls.__module__.startswith("sqlglot.") or cls.__getattribute__ is not object.__getattribute__
            or any("__init_subclass__" in vars(b) for b in cls.__mro__ if b is not object)):
            return cls
        excluded = set() if type(cls) is type else meta.reserved(type(cls))
        if excluded is not None:
            for name in set(names) - excluded:
                value = vars(cls).get(name)
                if type(value) is bool:
                    site = base.profile.site(f"guard:{cls.__module__}.{cls.__qualname__}:{name}")
                    base.profile.hit(site, "installed")
                    setattr(cls, name, Setting(site, value))
        return cls
    return decorate


@lru_cache(None)
def cached_field(cls, field):
    def body(func):
        tree = ast.parse(textwrap.dedent(inspect.getsource(func))).body[0]
        return [n for n in tree.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    try:
        prop = inspect.getattr_static(cls, field)
        if not isinstance(prop, property):
            return None
        ensure, ret = body(prop.fget)
        if not (isinstance(ret, ast.Return) and isinstance(ret.value, ast.Attribute)
                and isinstance(ret.value.value, ast.Name) and isinstance(ensure, ast.Expr)
                and isinstance(ensure.value, ast.Call) and isinstance(ensure.value.func, ast.Attribute)
                and ast.unparse(ensure.value.func.value) == ret.value.value.id
                and not ensure.value.args and not ensure.value.keywords):
            return None
        branch, = body(inspect.getattr_static(cls, ensure.value.func.attr))
        if (isinstance(branch, ast.If) and not branch.orelse and isinstance(branch.test, ast.UnaryOp)
            and isinstance(branch.test.op, ast.Not) and isinstance(branch.test.operand, ast.Attribute)
            and ast.unparse(branch.test.operand.value) == ret.value.value.id):
            return branch.test.operand.attr, ret.value.attr
    except (AttributeError, OSError, SyntaxError, TypeError, ValueError):
        pass
    return None


muted = ContextVar("suppressed_setting", default=None)


def guarded(obj, name, receiver=None, field=None, kind=None):
    cls = obj if isinstance(obj, type) else type(obj)
    descriptor = inspect.getattr_static(cls, name, None)
    if not isinstance(descriptor, Setting):
        if receiver is not None:
            base.profile.hit(base.profile.site("consumer:" + name), "possible")
        return getattr(obj, name)
    previous = muted.set(descriptor)
    try:
        value = getattr(obj, name)
    finally:
        muted.reset(previous)
    base.profile.hit(descriptor.site, "analyzed")
    if receiver is None:
        base.profile.hit(descriptor.site, "append")
        return value
    fields = vars(receiver) if getattr(type(receiver), "__dictoffset__", 0) else {}
    cache = cached_field(type(receiver), field) if type(receiver).__getattribute__ is object.__getattribute__ else None
    seq = fields.get(cache[1]) if cache and fields.get(cache[0]) is True else None
    if (type(seq) not in (list, tuple) or not isinstance(kind, type)
        or any(isinstance(item, kind) for item in seq)):
        base.profile.hit(descriptor.site, "read")
        base.profile.hit(base.profile.site("consumer:" + name), "possible")
    return value


def argument(value, callee, path, function, name):
    code = getattr(callee, "__code__", None)
    if code is None or code.co_name != function or code.co_filename != str(base.profile.root / path):
        base.profile.hit(base.profile.site("consumer:" + name), "possible")
    return value


class Transform(meta.Transform):
    def visit_Module(self, node):
        parents = {child: n for n in ast.walk(node) for child in ast.iter_child_nodes(n)}
        functions = {n.name: n for n in node.body if isinstance(n, ast.FunctionDef)}
        loops = {n: shape for n in ast.walk(node) if (shape := loop_shape(n)) and isolated_loop(n, parents)}
        edits = []
        for guard, (seq, kind, _) in loops.items():
            edits.append((guard, [seq.value, ast.Constant(seq.attr), kind]))
        for owner in functions.values():
            if inspects_locals(owner):
                continue
            for guard in ast.walk(owner):
                if not isinstance(guard, ast.If) or not isinstance(guard.test, ast.Attribute):
                    continue
                uses = private_append(guard, owner, parents, functions, loops)
                if uses:
                    edits.append((guard, []))
                    for call, pos, callee in uses:
                        ref = call.args[pos]
                        call.args[pos] = self.wrap(ref, "_rwx_arg", [ref, call.func, ast.Constant(self.path),
                                                    ast.Constant(callee.name), ast.Constant(guard.test.attr)])
        for guard, args in edits:
            test = guard.test
            guard.test = self.wrap(test, "_rwx_guard", [test.value, ast.Constant(test.attr), *args])
        return self.generic_visit(node)


if __name__ == "__main__":
    builtins._rwx_arg = argument
    builtins._rwx_guard = guarded
    builtins._rwx_scalars = install
    builtins._rwx_reflected = lambda obj: meta.namespace(obj, "all")
    builtins._rwx_ns = meta.namespace
    base.Transform = Transform
    base.Profile.entry = lambda self, code, offset: sys.monitoring.DISABLE
    base.Profile.audit = lambda self, event, args: None
    base.main()
