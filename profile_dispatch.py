"""Record baseline dispatch lookups, including absent keys and whole-table reads."""
import ast
import builtins
import sys

import profile_semantics as base

wrap = base.Transform.wrap


class DispatchTable(dict):
    def __init__(self, sites, values):
        super().__init__(values)
        self.sites = sites
        self.mark("registered")

    def mark(self, value):
        for site in self.sites:
            base.profile.hit(site, value)

    def record(self, key):
        self.mark("key:" + key if type(key) is str else "all")

    def __contains__(self, key):
        self.record(key)
        return super().__contains__(key)

    def __getitem__(self, key):
        self.record(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.record(key)
        return super().get(key, default)

    def __reduce__(self):
        self.mark("all")
        return DispatchTable, (self.sites, dict(dict.items(self)))


def whole_table(name):
    operation = getattr(dict, name)
    def observed(self, *args, **kwargs):
        self.mark("all")
        return operation(self, *args, **kwargs)
    return observed


for method in ("__iter__", "__reversed__", "__len__", "keys", "items", "values", "copy",
               "__or__", "__ror__", "__ior__", "__eq__", "__ne__", "__repr__", "pop",
               "popitem", "setdefault", "update", "clear", "__setitem__", "__delitem__"):
    setattr(DispatchTable, method, whole_table(method))


def merge_tables(site, *parts):
    sites = {site} if site is not None else set()
    result = {}
    for part in parts:
        if isinstance(part, DispatchTable):
            sites.update(part.sites)
            result.update(dict.items(part))
        else:
            result.update({**part})
    return DispatchTable(tuple(sites), result) if sites else result


def filtered(table, keep):
    if not isinstance(table, DispatchTable):
        return {k: v for k, v in table.items() if keep(k)}
    return DispatchTable(table.sites, {k: v for k, v in dict.items(table) if keep(k)})


class Transform(ast.NodeTransformer):
    def __init__(self, profile, path):
        self.profile, self.path = profile, path

    def visit_DictComp(self, node):
        self.generic_visit(node)
        if len(node.generators) != 1:
            return node
        gen = node.generators[0]
        call = gen.iter
        if not (isinstance(gen.target, ast.Tuple) and len(gen.target.elts) == 2
                and all(isinstance(n, ast.Name) for n in [*gen.target.elts, node.key, node.value])
                and [n.id for n in gen.target.elts] == [node.key.id, node.value.id]
                and not gen.is_async and len(gen.ifs) == 1 and isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute) and call.func.attr == "items"
                and not call.args and not call.keywords):
            return node
        cond = gen.ifs[0]
        if not (isinstance(cond, ast.Compare) and len(cond.ops) == 1
                and isinstance(cond.ops[0], (ast.Eq, ast.NotEq, ast.In, ast.NotIn))
                and isinstance(cond.left, ast.Name) and cond.left.id == node.key.id):
            return node
        try:
            ast.literal_eval(cond.comparators[0])
        except (ValueError, TypeError):
            return node
        params = ast.arguments(posonlyargs=[], args=[ast.arg(node.key.id)], kwonlyargs=[],
                               kw_defaults=[], defaults=[])
        return wrap(node, "_rwx_filtered", [call.func.value, ast.Lambda(params, cond)])

    def visit_ClassDef(self, node):
        self.generic_visit(node)
        for stmt in node.body:
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or not isinstance(stmt.value, ast.Dict):
                continue
            literal = stmt.value
            tracked = any(isinstance(k, ast.Constant) and type(k.value) is str and isinstance(v, ast.Lambda)
                          for k, v in zip(literal.keys, literal.values))
            if not tracked and None not in literal.keys:
                continue
            site = self.profile.site(f"{self.path}:{literal.lineno}:{literal.col_offset}:dispatch") if tracked else None
            parts = []
            keys, values = [], []
            for key, value in zip(literal.keys, literal.values):
                if key is None:
                    if keys:
                        parts.append(ast.Dict(keys, values))
                    parts.append(value)
                    keys, values = [], []
                else:
                    keys.append(key)
                    values.append(value)
            if keys:
                parts.append(ast.Dict(keys, values))
            stmt.value = wrap(literal, "_rwx_dispatch", [ast.Constant(site), *parts])
        return node


if __name__ == "__main__":
    builtins._rwx_dispatch = merge_tables
    builtins._rwx_filtered = filtered
    base.Transform = Transform
    base.Profile.entry = lambda self, code, offset: sys.monitoring.DISABLE
    base.Profile.audit = lambda self, event, args: None
    base.main()
