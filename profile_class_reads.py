"""Trace scalar settings for classes built by a transparent Python metaclass."""
import ast
import builtins
from functools import lru_cache
import inspect
import sys
import textwrap

import profile_scalars as scalars
import profile_semantics as base


@lru_cache(None)
def reserved(meta):
    if meta.__bases__ != (type,) or set(vars(meta)).intersection({
        "__init__", "__getattr__", "__getattribute__", "__setattr__", "__prepare__", "__call__"
    }):
        return None
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(meta.__new__))).body[0]
    except (OSError, TypeError, SyntaxError):
        return None
    if not isinstance(tree, ast.FunctionDef) or len(tree.args.args) != 4:
        return None
    first = tree.body[0]
    if not (isinstance(first, ast.Assign) and len(first.targets) == 1
            and isinstance(first.targets[0], ast.Name) and isinstance(first.value, ast.Call)
            and ast.unparse(first.value.func) == "super().__new__"
            and [ast.unparse(a) for a in first.value.args] == [a.arg for a in tree.args.args]):
        return None
    cls, namespace = first.targets[0].id, tree.args.args[-1].arg
    names = set()
    for stmt in tree.body[1:]:
        parents = {child: n for n in ast.walk(stmt) for child in ast.iter_child_nodes(n)}
        for n in ast.walk(stmt):
            if isinstance(n, ast.Name) and n.id == namespace:
                return None
            if isinstance(n, ast.Name) and n.id == cls:
                parent = parents.get(n)
                if isinstance(parent, ast.Attribute):
                    names.add(parent.attr)
                    if parent.attr == "__dict__":
                        method = parents.get(parent)
                        call = parents.get(method)
                        if not (isinstance(method, ast.Attribute) and method.attr == "get"
                                and isinstance(call, ast.Call) and call.args
                                and isinstance(call.args[0], ast.Constant)):
                            return None
                        names.add(call.args[0].value)
                elif not isinstance(parent, ast.Return) and not (
                    isinstance(parent, ast.Assign) and parent.value is n and
                    all(isinstance(target, ast.Subscript) for target in parent.targets)
                ):
                    return None
    return names


def install_settings(names):
    def install(cls):
        if (not cls.__module__.startswith("sqlglot.") or cls.__getattribute__ is not object.__getattribute__
            or any("__init_subclass__" in vars(b) for b in cls.__mro__ if b is not object)):
            return cls
        excluded = reserved(type(cls))
        if excluded is None:
            return cls
        for name in set(names) - excluded:
            value = vars(cls).get(name)
            if type(value) in (bool, int, str):
                site = base.profile.site(f"metascalar:{cls.__module__}.{cls.__qualname__}:{name}")
                base.profile.hit(site, "installed")
                setattr(cls, name, scalars.Scalar(site, value))
        return cls
    return install


def namespace(obj, key):
    if isinstance(obj, type) and obj.__module__.startswith("sqlglot."):
        site = base.profile.site(f"classdict:{obj.__module__}.{obj.__qualname__}")
        base.profile.hit(site, key)
    return obj


class Transform(scalars.Transform):
    def visit_Call(self, node):
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "get" and node.args
            and isinstance(node.args[0], ast.Constant) and type(node.args[0].value) is str
            and isinstance(func.value, ast.Attribute) and func.value.attr == "__dict__"):
            field = func.value
            self.generic_visit(field.value)
            field.value = self.wrap(field.value, "_rwx_ns", [field.value, ast.Constant("key:" + node.args[0].value)])
            node.args = [self.visit(a) for a in node.args]
            node.keywords = [self.visit(k) for k in node.keywords]
            return node
        return super().visit_Call(node)

    def visit_Compare(self, node):
        return self.generic_visit(node)


if __name__ == "__main__":
    builtins._rwx_scalars = install_settings
    builtins._rwx_reflected = lambda obj: namespace(obj, "all")
    builtins._rwx_ns = namespace
    base.Transform = Transform
    base.Profile.entry = lambda self, code, offset: sys.monitoring.DISABLE
    base.Profile.audit = lambda self, event, args: None
    base.main()
