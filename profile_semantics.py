"""Collect operand, callback, attribute and source-read coverage on pristine SQLGlot."""
import argparse
import ast
import builtins
import concurrent.futures
import dis
import enum
import functools
import gzip
import importlib.machinery
import json
import operator
import os
from pathlib import Path
import sys
import types

import pytest
from build_map import MeasuredPool


COMPARE = {"Is": operator.is_, "IsNot": operator.is_not, "Eq": operator.eq,
           "NotEq": operator.ne, "Lt": operator.lt, "LtE": operator.le,
           "Gt": operator.gt, "GtE": operator.ge}


def observation(left, right):
    """Identity and equality facts. Never invoke user-defined comparison methods."""
    a, b = type(left), type(right)
    identity = left is right
    primitive = (int, bool, str, bytes, float, type(None))
    if a in primitive and b in primitive:
        equal = left == right
    elif a is tuple and b is tuple and all(type(v) is int for v in (*left, *right)):
        equal = left == right
    elif isinstance(left, enum.Enum) and a is b and a.__eq__ in (str.__eq__, int.__eq__, object.__eq__):
        equal = left == right
    elif all(t is type(None) or t.__eq__ is object.__eq__ for t in (a, b)):
        equal = identity
    else:
        return "?"
    return ("1" if identity else "0") + ("1" if equal else "0")


class ObservedDict(dict):
    def __init__(self, site, values):
        super().__init__(values)
        self.site = site
        profile.hit(site, "registered")

    def __getitem__(self, key):
        profile.hit(self.site, "v:" + repr(key))
        return super().__getitem__(key)

    def get(self, key, default=None):
        profile.hit(self.site, "v:" + repr(key))
        return super().get(key, default)

    def __iter__(self):
        return super().__iter__()

    def __reduce__(self):
        return ObservedDict, (self.site, dict(dict.items(self)))


def broad_read(name):
    method = getattr(dict, name)
    def read(self, *args, **kwargs):
        profile.hit(self.site, "all")
        return method(self, *args, **kwargs)
    return read


for method in ("items", "values", "copy", "__or__", "__ror__", "__eq__", "__ne__",
               "__repr__", "pop", "popitem", "setdefault"):
    setattr(ObservedDict, method, broad_read(method))


class Transform(ast.NodeTransformer):
    def __init__(self, profile, path):
        self.p = profile
        self.path = path

    def site(self, node, kind):
        return self.p.site(f"{self.path}:{node.lineno}:{node.col_offset}:{kind}")

    @staticmethod
    def wrap(node, helper, args):
        return ast.copy_location(ast.Call(ast.Name(helper, ast.Load()), args, []), node)

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1:
            return node
        op = type(node.ops[0]).__name__
        if op not in COMPARE:
            return node
        return self.wrap(node, "_rwx_compare", [ast.Constant(self.site(node, "cmp")),
                         ast.Constant(op), node.left, node.comparators[0]])

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and (node.attr.isupper() or node.attr == "arg_types" or node.attr in self.p.derived_names):
            return self.wrap(node, "_rwx_attribute", [node.value, ast.Constant(node.attr)])
        return node

    def visit_Assign(self, node):
        # Recognize a derived key set computed from the truth of dictionary values.
        # Preserve its native type and follow subsequent reads of the derived class attribute.
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Attribute) and isinstance(node.value, ast.SetComp):
            target, comp = node.targets[0], node.value
            if len(comp.generators) == 1 and isinstance(target.value, ast.Name):
                gen = comp.generators[0]
                call = gen.iter
                if (not gen.is_async and isinstance(gen.target, ast.Tuple) and len(gen.target.elts) == 2
                    and all(isinstance(x, ast.Name) for x in gen.target.elts)
                    and isinstance(comp.elt, ast.Name) and comp.elt.id == gen.target.elts[0].id
                    and len(gen.ifs) == 1 and isinstance(gen.ifs[0], ast.Name) and gen.ifs[0].id == gen.target.elts[1].id
                    and isinstance(call, ast.Call) and not call.args and not call.keywords
                    and isinstance(call.func, ast.Attribute) and call.func.attr == "items"
                    and isinstance(call.func.value, ast.Attribute) and isinstance(call.func.value.value, ast.Name)
                    and target.value.id == call.func.value.value.id):
                    self.p.derived_names.add(target.attr)
                    node.value = self.wrap(comp, "_rwx_keyset", [target.value, ast.Constant(target.attr), call.func.value])
                    return node
        return self.generic_visit(node)

    def visit_ClassDef(self, node):
        self.generic_visit(node)
        for stmt in node.body:
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or not isinstance(stmt.value, ast.Dict):
                continue
            literal = stmt.value
            try:
                values = ast.literal_eval(literal)
            except (ValueError, TypeError, SyntaxError):
                pass
            else:
                if values and all(isinstance(k, (int, str)) for k in values):
                    stmt.value = self.wrap(literal, "_rwx_mapping", [ast.Constant(self.site(literal, "mapping")), literal])
                    continue
            for i, value in enumerate(stmt.value.values):
                if isinstance(value, (ast.Call, ast.Lambda)) and stmt.value.keys[i] is not None:
                    stmt.value.values[i] = self.wrap(value, "_rwx_callback",
                        [ast.Constant(self.site(value, "callback")), value])
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        profile.loading += 1
        try:
            path = Path(self.path)
            tree = ast.parse(path.read_bytes(), self.path)
            tree = Transform(profile, path.relative_to(profile.root).as_posix()).visit(tree)
            return compile(ast.fix_missing_locations(tree), self.path, "exec", dont_inherit=True)
        finally:
            profile.loading -= 1


class Finder:
    def find_spec(self, fullname, path, target=None):
        if fullname.split(".", 1)[0] not in {"sqlglot", "tests"}:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and spec.origin and spec.origin.endswith(".py") and Path(spec.origin).is_relative_to(profile.root):
            spec.loader = Loader(fullname, spec.origin)
        return spec


class Profile:
    def __init__(self, root, ids):
        self.root, self.ids = root, ids
        self.indices = {name: i for i, name in enumerate(ids)}
        self.startup = 1 << len(ids)
        self.mask = self.startup
        self.sites = {}
        self.data = []
        self.seen = set()
        self.loading = 0
        self.passed = set()
        self.failed = set()
        self.derived_names = set()
        self.derived = {}
        self.selected = set(ids)

    def site(self, key):
        index = self.sites.get(key)
        if index is None:
            index = self.sites[key] = len(self.data)
            self.data.append({})
        return index

    def hit(self, site, value):
        key = (site, value)
        if key not in self.seen:
            self.seen.add(key)
            facts = self.data[site]
            facts[value] = facts.get(value, 0) | self.mask

    def compare(self, site, op, left, right):
        result = COMPARE[op](left, right)
        self.hit(site, observation(left, right))
        return result

    @functools.lru_cache(maxsize=None)
    def owner(self, cls, name):
        for base in cls.__mro__:
            if name in vars(base):
                if base.__module__.startswith("sqlglot."):
                    return self.site(f"attr:{base.__module__}.{base.__qualname__}:{name}")
                break
        return None

    def attribute(self, obj, name):
        value = getattr(obj, name)
        if not isinstance(obj, types.ModuleType):
            cls = obj if isinstance(obj, type) else type(obj)
            if name in self.derived_names:
                for base in cls.__mro__:
                    if name in vars(base):
                        derived = self.derived.get((base, name))
                        if derived is not None:
                            self.hit(derived, "all")
                        break
            site = self.owner(cls, name)
            if site is not None:
                self.hit(site, "read")
        return value

    def keyset(self, owner, name, mapping):
        if isinstance(mapping, ObservedDict) and isinstance(owner, type):
            self.derived[owner, name] = mapping.site
            return {k for k, v in dict.items(mapping) if v}
        return {k for k, v in mapping.items() if v}

    def callback(self, site, value):
        if not isinstance(value, types.FunctionType):
            self.hit(site, "opaque")
            return value
        self.hit(site, "registered")
        @functools.wraps(value)
        def call(*args, **kwargs):
            self.hit(site, "called")
            return value(*args, **kwargs)
        return call

    @functools.lru_cache(maxsize=None)
    def relative(self, name):
        if not isinstance(name, str) or name.startswith("<"):
            return None
        path = Path(os.path.abspath(name))
        return path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else None

    def entry(self, code, offset):
        path = self.relative(code.co_filename)
        if path and path.startswith(("sqlglot/", "tests/")):
            self.hit(self.site(f"fn:{path}:{code.co_firstlineno}:{code.co_qualname}"), "ran")
        return sys.monitoring.DISABLE

    def audit(self, event, args):
        if event == "open" and not self.loading:
            path = self.relative(args[0])
            if path and path.startswith(("sqlglot/", "tests/")) and path.endswith(".py"):
                self.hit(self.site("read:" + path), "read")

    def pytest_collection_modifyitems(self, items):
        items[:] = [item for item in items if item.nodeid in self.selected]

    def pytest_runtest_logstart(self, nodeid, location):
        self.mask = 1 << self.indices[nodeid]
        self.seen.clear()
        sys.monitoring.restart_events()

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)
        elif report.when == "call" and report.passed:
            self.passed.add(report.nodeid)

    def pytest_runtest_logfinish(self, nodeid, location):
        self.mask = self.startup
        self.seen.clear()

    def save(self, dest):
        masks = {}
        facts = {s: {v: masks.setdefault(m, len(masks)) for v, m in self.data[i].items()}
                 for s, i in self.sites.items()}
        result = {"ids": self.ids, "facts": facts, "masks": [format(m, "x") for m in masks],
                  "unverified": sorted(set(self.ids) - (self.passed - self.failed))}
        with gzip.open(dest, "wt", encoding="utf8") as f:
            json.dump(result, f, separators=(",", ":"))


def main():
    global profile
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "catalog", "output"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--shard", type=int, default=0)
    a = parser.parse_args()
    root, dest = a.source.resolve(), a.output.resolve()
    tests = json.loads(a.catalog.read_bytes())["tests"]
    ids = sorted(t["id"] for t in tests)
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    profile = Profile(root, ids)
    if not 0 <= a.shard < a.shards:
        parser.error("invalid shard")
    loads = [0.0] * a.shards
    selected = set()
    for test in sorted(tests, key=lambda t: (-t["seconds"], t["id"])):
        lane = loads.index(min(loads))
        loads[lane] += test["seconds"]
        if lane == a.shard:
            selected.add(test["id"])
    profile.selected = selected
    builtins._rwx_mapping = ObservedDict
    builtins._rwx_keyset = profile.keyset
    builtins._rwx_compare = profile.compare
    builtins._rwx_callback = profile.callback
    builtins._rwx_attribute = profile.attribute
    concurrent.futures.ProcessPoolExecutor = MeasuredPool
    sys.meta_path.insert(0, Finder())
    sys.addaudithook(profile.audit)
    mon = sys.monitoring
    mon.use_tool_id(mon.COVERAGE_ID, "baseline semantic evidence")
    mon.register_callback(mon.COVERAGE_ID, mon.events.PY_START, profile.entry)
    mon.set_events(mon.COVERAGE_ID, mon.events.PY_START)
    try:
        status = pytest.main(["-q", "--disable-warnings", "--continue-on-collection-errors", "tests"], plugins=[profile])
    finally:
        mon.set_events(mon.COVERAGE_ID, 0)
        mon.free_tool_id(mon.COVERAGE_ID)
    if status not in (0, 1) or not profile.passed:
        raise SystemExit(f"Incomplete test profile ({status})")
    profile.save(dest)


if __name__ == "__main__":
    main()
