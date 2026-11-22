"""Measure conditional outcomes and certified empty native iteration on pristine tests."""
import ast
import builtins
import operator
from functools import lru_cache
import sys
import types

import profile_semantics as base


UNKNOWN = object()
SCALARS = (type(None), bool, int, float, str, bytes)
CONTAINERS = (dict, list, tuple, set, frozenset, str, bytes, range)
cache_getters = {}


@lru_cache(None)
def descriptor(cls, name):
    if type(cls).__getattribute__ is not type.__getattribute__ or cls.__getattribute__ is not object.__getattribute__:
        return UNKNOWN
    for parent in cls.__mro__:
        if name in vars(parent):
            return vars(parent)[name]
    return None


def stored(obj, name):
    member = descriptor(type(obj), name)
    if member is UNKNOWN:
        return UNKNOWN
    if type(member) is types.MemberDescriptorType:
        try:
            return member.__get__(obj)
        except AttributeError:
            return UNKNOWN
    if member is not None and hasattr(type(member), "__get__"):
        return UNKNOWN
    storage = descriptor(type(obj), "__dict__")
    if type(storage) is types.GetSetDescriptorType:
        values = storage.__get__(obj)
        if type(values) is dict and name in values:
            return values[name]
    return member if member is not None else UNKNOWN


def encode(node):
    if isinstance(node, ast.Constant) and type(node.value) in SCALARS:
        return ("literal", node.value)
    if isinstance(node, ast.Name):
        return ("name", node.id)
    if isinstance(node, ast.Attribute) and (parent := encode(node.value)):
        return ("attr", parent, node.attr)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
        and len(node.args) == 1 and not node.keywords and isinstance(node.args[0], ast.Constant)
        and type(node.args[0].value) is str and (parent := encode(node.func.value))):
        return ("get", parent, node.args[0].value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not) and (parent := encode(node.operand)):
        return ("not", parent)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)):
        a, b = encode(node.left), encode(node.comparators[0])
        if a and b:
            return (type(node.ops[0]).__name__, a, b)


def safe_truth(v):
    if type(v) in SCALARS + CONTAINERS:
        return bool(v)
    return UNKNOWN


def evaluate(expr, locals_):
    op, *args = expr
    if op == "literal":
        return args[0]
    if op == "name":
        return locals_.get(args[0], UNKNOWN)
    x = evaluate(args[0], locals_)
    if x is UNKNOWN:
        return UNKNOWN
    if op == "attr":
        return stored(x, args[1])
    if op == "get":
        if type(x) is dict and all(type(k) is str for k in x):
            return x.get(args[1])
        return UNKNOWN
    if op == "not":
        truth = safe_truth(x)
        return UNKNOWN if truth is UNKNOWN else not truth
    y = evaluate(args[1], locals_)
    if y is UNKNOWN:
        return UNKNOWN
    if op in ("Is", "IsNot"):
        return (x is y) == (op == "Is")
    if type(x) in SCALARS and type(y) in SCALARS:
        return (x == y) == (op == "Eq")
    return UNKNOWN


def branch(left, plan, site):
    p = base.profile
    p.hit(site, "ran")
    if (site, "different") not in p.seen:
        l = safe_truth(left)
        r = safe_truth(evaluate(plan, sys._getframe(1).f_locals)) if l is not UNKNOWN else UNKNOWN
        if l is UNKNOWN or r is UNKNOWN or l != r:
            p.hit(site, "different")
    return left


def filtered(site, op, and_, left, right, plan):
    p = base.profile
    p.hit(site, "ran")
    if (site, "possible") not in p.seen:
        def ordinary(v):
            cls = type(v)
            return (getattr(cls, "__eq__", None), getattr(cls, "__ne__", None)) in (
                (int.__eq__, int.__ne__), (str.__eq__, str.__ne__), (float.__eq__, float.__ne__),
                (bytes.__eq__, bytes.__ne__), (object.__eq__, object.__ne__))
        tail = evaluate(plan, sys._getframe(1).f_locals)
        if not ordinary(left) or not ordinary(right) or tail is not (False if and_ else True):
            p.hit(site, "possible")
    return (operator.eq if op == "Eq" else operator.ne)(left, right)


def count(func, site, mode, name, owner):
    value = getattr(owner, name) if name else owner
    result = func(value)
    p = base.profile
    p.hit(site, "ran")
    if result != 0 or func is not len:
        if func is not len:
            p.hit(site, "unknown")
        return result
    stable = type(value) is dict if mode else type(value) in CONTAINERS
    if stable and name:
        member = descriptor(type(owner), name)
        if type(member) is property:
            code = getattr(member.fget, "__code__", None)
            key = (code.co_filename, code.co_firstlineno) if code else None
            name = cache_getters.get(key)
        stable = bool(name) and stored(owner, name) is value
    if not stable:
        p.hit(site, "unknown")
    return result


class Transform(ast.NodeTransformer):
    wrap = staticmethod(base.Transform.wrap)

    def __init__(self, profile, path):
        self.p, self.path = profile, path

    def visit_FunctionDef(self, node):
        body = [s for s in node.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                                              and type(s.value.value) is str)]
        if (len(node.decorator_list) == 1 and isinstance(node.decorator_list[0], ast.Name)
            and node.decorator_list[0].id == "property" and len(body) == 2 and isinstance(body[0], ast.If)
            and not body[0].orelse and isinstance(body[1], ast.Return)):
            val, cond = body[1].value, body[0].test
            if (isinstance(val, ast.Attribute) and isinstance(val.value, ast.Name) and node.args.args
                and val.value.id == node.args.args[0].arg and isinstance(cond, ast.Compare)
                and len(cond.ops) == 1 and isinstance(cond.ops[0], ast.Is)
                and ast.dump(cond.left) == ast.dump(val) and isinstance(cond.comparators[0], ast.Constant)
                and cond.comparators[0].value is None):
                cache_getters[str(self.p.root / self.path), node.decorator_list[0].lineno] = val.attr
        return self.generic_visit(node)

    def condition(self, node):
        self.generic_visit(node)
        test = node.test
        if isinstance(test, ast.BoolOp) and len(test.values) == 2 and (plan := encode(test.values[1])):
            site = self.p.site(f"branch:{self.path}:{test.lineno}:{test.col_offset}")
            test.values[0] = self.wrap(test.values[0], "_rwx_branch", [test.values[0], ast.Constant(plan), ast.Constant(site)])
        return node

    visit_If = visit_While = visit_IfExp = condition

    def visit_BoolOp(self, node):
        first = node.values[0]
        if (len(node.values) == 2 and isinstance(first, ast.Compare) and len(first.ops) == 1
            and isinstance(first.ops[0], (ast.Eq, ast.NotEq)) and (plan := encode(node.values[1]))):
            site = self.p.site(f"masked:{self.path}:{first.lineno}:{first.col_offset}")
            node.values[0] = self.wrap(first, "_rwx_filtered", [
                ast.Constant(site), ast.Constant(type(first.ops[0]).__name__), ast.Constant(isinstance(node.op, ast.And)),
                first.left, first.comparators[0], ast.Constant(plan)])
        return self.generic_visit(node)

    def visit_For(self, node):
        if len(node.body) >= 2:
            guard, loop = node.body[-2:]
            if (isinstance(guard, ast.If) and len(guard.body) == 1 and isinstance(guard.body[0], ast.Continue)
                and not guard.orelse and isinstance(loop, ast.For) and not loop.orelse
                and isinstance(guard.test, ast.Compare)):
                call, it = guard.test.left, loop.iter
                mode = isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute) and it.func.attr in ("items","keys","values") and not it.args and not it.keywords
                target = it.func.value if mode else it
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "len"
                    and len(call.args) == 1 and not call.keywords and ast.dump(call.args[0]) == ast.dump(target)
                    and (isinstance(target, ast.Name) or isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name))):
                    obj = call.args[0]
                    name = obj.attr if isinstance(obj, ast.Attribute) else None
                    site = self.p.site(f"empty:{self.path}:{guard.test.lineno}:{guard.test.col_offset}")
                    guard.test.left = self.wrap(call, "_rwx_count", [call.func, ast.Constant(site), ast.Constant(mode),
                                                                     ast.Constant(name), obj.value if name else obj])
        return self.generic_visit(node)


if __name__ == "__main__":
    builtins._rwx_branch = branch
    builtins._rwx_filtered = filtered
    builtins._rwx_count = count
    base.Transform = Transform
    base.Profile.entry = lambda self, code, offset: sys.monitoring.DISABLE
    base.Profile.audit = lambda self, event, args: None
    base.main()
