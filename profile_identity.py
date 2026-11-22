"""Measure identity against None when the alternative equality operator is provably pure."""
import ast
import builtins
from functools import lru_cache
import inspect
import operator
import sys
import textwrap
import types

import profile_semantics as base


UNKNOWN = object()
NATIVE = (type(None), bool, int, float, complex, bytes, str, tuple, list, dict, set, frozenset, range, bytearray)


@lru_cache(None)
def body(fn):
    if type(fn) is not types.FunctionType:
        return None
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
        if (isinstance(tree, ast.FunctionDef) and not tree.decorator_list and len(tree.args.args) == 2
            and not tree.args.posonlyargs and fn.__globals__.get("type", type) is type):
            return tree, [n.arg for n in tree.args.args]
    except (SyntaxError, TypeError, OSError):
        return None


def partial(n, names):
    if isinstance(n, ast.Constant) and type(n.value) is bool:
        return n.value
    if isinstance(n, ast.BoolOp):
        for part in n.values:
            outcome = partial(part, names)
            if outcome is UNKNOWN or (outcome is False and isinstance(n.op, ast.And)) or (outcome is True and isinstance(n.op, ast.Or)):
                return outcome
        return outcome
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
        result = partial(n.operand, names)
        return UNKNOWN if result is UNKNOWN else not result
    if isinstance(n, ast.Compare) and len(n.ops) == 1 and isinstance(n.ops[0], (ast.Is, ast.IsNot)):
        left, right = n.left, n.comparators[0]
        if all(isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id == "type"
               and len(x.args) == 1 and not x.keywords for x in (left, right)):
            left, right = left.args[0], right.args[0]
        if isinstance(left, ast.Name) and isinstance(right, ast.Name) and {left.id,right.id} == set(names):
            return isinstance(n.ops[0], ast.IsNot)
    return UNKNOWN


def bound_eq(value, cls, fn):
    if cls.__getattribute__ is not object.__getattribute__:
        return False
    method = getattr(value, "__eq__", None)
    return type(method) is types.MethodType and method.__func__ is fn


@lru_cache(None)
def guarded(fn):
    model = body(fn)
    if model:
        tree, names = model
        if len(tree.body) == 1 and isinstance(tree.body[0], ast.Return):
            return partial(tree.body[0].value, names)
    return UNKNOWN


@lru_cache(None)
def negates(fn):
    model = body(fn)
    if model:
        tree, (self_, other) = model
        if len(tree.body) == 1 and isinstance(tree.body[0], ast.Return):
            expr = tree.body[0].value
            if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
                call = expr.operand
                return (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "__eq__"
                        and isinstance(call.func.value, ast.Name) and call.func.value.id == self_ and len(call.args) == 1
                        and isinstance(call.args[0], ast.Name) and call.args[0].id == other and not call.keywords)
    return False


def safe(value, negative):
    cls = type(value)
    if cls in NATIVE:
        return True
    eq, ne = cls.__eq__, cls.__ne__
    # A subclass can inherit the primitive comparison implementation unchanged.
    for builtin in NATIVE:
        if eq is builtin.__eq__ and (not negative or ne is builtin.__ne__):
            return True
    if eq is object.__eq__ and (not negative or ne is object.__ne__):
        return True
    return guarded(eq) is False and (not negative or ne is object.__ne__
                                      or negates(ne) and bound_eq(value, cls, eq))


def observe(site, op, x, y):
    p = base.profile
    p.hit(site, "ran")
    if (site, "possible") not in p.seen and not safe(y if x is None else x, op in ("IsNot", "NotEq")):
        p.hit(site, "possible")
    return OPS[op](x,y)


OPS = {"Is": operator.is_, "IsNot": operator.is_not, "Eq": operator.eq, "NotEq": operator.ne}


class Transform(ast.NodeTransformer):
    wrap = staticmethod(base.Transform.wrap)
    def __init__(self, profile, path):
        self.p, self.path = profile, path

    def visit_Compare(self, node):
        self.generic_visit(node)
        if (len(node.ops) == 1 and (op := type(node.ops[0]).__name__) in OPS
            and any(isinstance(v, ast.Constant) and v.value is None for v in (node.left, node.comparators[0]))):
            site = self.p.site(f"none:{self.path}:{node.lineno}:{node.col_offset}")
            return self.wrap(node, "_rwx_none", [ast.Constant(site), ast.Constant(op), node.left, node.comparators[0]])
        return node


if __name__ == "__main__":
    builtins._rwx_none = observe
    base.Transform = Transform
    base.Profile.entry = lambda self, code, offset: sys.monitoring.DISABLE
    base.Profile.audit = lambda self, event, args: None
    base.main()
