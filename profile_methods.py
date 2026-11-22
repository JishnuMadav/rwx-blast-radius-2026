"""Trace baseline reads of ordinary private methods and class namespaces."""
import ast
import builtins
from functools import lru_cache
import sys
import types

import profile_semantics as base


@lru_cache(None)
def owners(cls):
    return tuple(c for c in cls.__mro__ if c.__module__.startswith("sqlglot."))


@lru_cache(None)
def method_sites(cls, key):
    return tuple(base.profile.site(f"method:{c.__module__}.{c.__qualname__}.{key}")
                 for c in owners(cls) if isinstance(vars(c).get(key), types.FunctionType))


def cls_of(obj):
    if isinstance(obj, super):
        obj = obj.__self_class__
    return obj if isinstance(obj, type) else type(obj)


def note(obj, key):
    if type(key) is str and key.startswith("_") and not key.startswith("__"):
        for site in method_sites(cls_of(obj), key):
            base.profile.hit(site, "read")


def attribute(obj, name):
    note(obj, name)
    return getattr(obj, name)


def get(func, obj, name, *default):
    note(obj, name)
    return func(obj, name, *default)


def raw(obj):
    for c in owners(cls_of(obj)):
        base.profile.hit(base.profile.site(f"method-dict:{c.__module__}.{c.__qualname__}"), "read")
    return obj


def register(names):
    def record(cls):
        if (type(cls) is not type or cls.__getattribute__ is not object.__getattribute__
            or any("__init_subclass__" in vars(c) for c in cls.__mro__ if c is not object)):
            return cls
        for name in names:
            if isinstance(vars(cls).get(name), types.FunctionType):
                site = base.profile.site(f"method:{cls.__module__}.{cls.__qualname__}.{name}")
                base.profile.hit(site, "registered")
        return cls
    return record


class Transform(ast.NodeTransformer):
    wrap = staticmethod(base.Transform.wrap)
    def __init__(self, profile, path):
        self.path = path

    def visit_ClassDef(self, node):
        names = []
        for child in node.body:
            if isinstance(child, ast.FunctionDef) and not child.decorator_list and child.name.startswith("_") and not child.name.startswith("__"):
                names.append(child.name)
        # A bare name used in a class body can escape before the class exists.
        escapes = {n.id for stmt in node.body if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                   for n in ast.walk(stmt) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        self.generic_visit(node)
        if self.path.startswith("sqlglot/") and not node.decorator_list and names:
            names = sorted(set(names) - escapes)
            node.decorator_list.append(self.wrap(node, "_rwx_register_methods", [ast.Constant(tuple(names))]))
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load):
            if node.attr == "__dict__":
                node.value = self.wrap(node.value, "_rwx_method_raw", [node.value])
            elif node.attr.startswith("_") and not node.attr.startswith("__"):
                return self.wrap(node, "_rwx_method_attr", [node.value, ast.Constant(node.attr)])
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and not node.keywords and not any(isinstance(a, ast.Starred) for a in node.args):
            if node.func.id in ("getattr", "hasattr") and len(node.args) in (2, 3):
                return self.wrap(node, "_rwx_method_get", [node.func, *node.args])
            if node.func.id in ("vars", "dir") and len(node.args) == 1:
                node.args[0] = self.wrap(node.args[0], "_rwx_method_raw", [node.args[0]])
        return node


if __name__ == "__main__":
    builtins._rwx_register_methods = register
    builtins._rwx_method_raw = raw
    builtins._rwx_method_attr = attribute
    builtins._rwx_method_get = get
    base.Transform = Transform
    base.Profile.entry = lambda self, code, offset: sys.monitoring.DISABLE
    base.Profile.audit = lambda self, event, args: None
    base.main()
