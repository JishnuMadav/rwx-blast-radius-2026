"""Observe plain class settings and integer operands without recording any changed patches."""
import ast
import builtins
import sys

import profile_semantics as base


class Scalar:
    def __init__(self, site, value):
        self.site = site
        self.value = value

    def __get__(self, obj, owner=None):
        base.profile.hit(self.site, "read")
        return self.value


def instrument_class(names):
    def install(cls):
        if (type(cls) is not type or not cls.__module__.startswith("sqlglot.")
            or cls.__getattribute__ is not object.__getattribute__
            or any("__init_subclass__" in parent.__dict__ for parent in cls.__mro__ if parent is not object)):
            return cls
        for name in names:
            value = vars(cls).get(name)
            if type(value) not in (bool, int, str):
                continue
            site = base.profile.site(f"scalar:{cls.__module__}.{cls.__qualname__}:{name}")
            base.profile.hit(site, "installed")
            setattr(cls, name, Scalar(site, value))
        return cls
    return install


def reflected(obj):
    if isinstance(obj, type) and obj.__module__.startswith("sqlglot."):
        base.profile.hit(base.profile.site(f"namespace:{obj.__module__}.{obj.__qualname__}"), "read")
    return obj


def number(site, value):
    key = str(value) if type(value) is int and -128 <= value <= 128 else "unknown"
    base.profile.hit(site, key)
    return value


class Transform(ast.NodeTransformer):
    wrap = staticmethod(base.Transform.wrap)
    def __init__(self, profile, path):
        self.p, self.path = profile, path

    def visit_ClassDef(self, node):
        names = []
        raw_reads = {n.id for stmt in node.body for n in ast.walk(stmt)
                     if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        if not node.decorator_list:
            for stmt in node.body:
                target = stmt.targets[0] if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 else getattr(stmt, "target", None)
                if (isinstance(target, ast.Name) and target.id.isupper() and target.id not in raw_reads
                    and isinstance(stmt, (ast.Assign, ast.AnnAssign)) and isinstance(stmt.value, ast.Constant)
                    and type(stmt.value.value) in (bool, int, str)):
                    names.append(target.id)
        self.generic_visit(node)
        if names:
            node.decorator_list.append(ast.copy_location(ast.Call(ast.Name("_rwx_scalars", ast.Load()),
                                       [ast.Constant(tuple(names))], []), node))
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if node.attr == "__dict__" and isinstance(node.ctx, ast.Load):
            node.value = base.Transform.wrap(node.value, "_rwx_reflected", [node.value])
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        if (isinstance(node.func, ast.Name) and node.func.id == "vars" and len(node.args) == 1
            and not isinstance(node.args[0], ast.Starred)):
            node.args[0] = base.Transform.wrap(node.args[0], "_rwx_reflected", [node.args[0]])
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        if (len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq, ast.Lt, ast.Gt, ast.LtE, ast.GtE))
            and isinstance(node.comparators[0], ast.Constant) and type(node.comparators[0].value) is int):
            site = self.p.site(f"number:{self.path}:{node.lineno}:{node.col_offset}")
            node.left = base.Transform.wrap(node.left, "_rwx_number", [ast.Constant(site), node.left])
        return node


if __name__ == "__main__":
    builtins._rwx_scalars = instrument_class
    builtins._rwx_reflected = reflected
    builtins._rwx_number = number
    base.Transform = Transform
    base.Profile.entry = lambda self, code, offset: sys.monitoring.DISABLE
    base.Profile.audit = lambda self, event, args: None
    base.main()
