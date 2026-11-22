"""Attribute startup settings reads in pristine, independently imported catalog files."""
import argparse
import ast
import builtins
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import gzip
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import profile_class_reads as meta
import profile_scalars as scalar
import profile_semantics as base


class Transform(meta.Transform):
    def visit_Attribute(self, node):
        super().visit_Attribute(node)
        if isinstance(node.ctx, ast.Load) and (node.attr.isupper() or node.attr == "arg_types"):
            return self.wrap(node, "_rwx_cold_attr", [node.value, ast.Constant(node.attr)])
        return node


def install(names):
    def decorate(cls):
        return (scalar.instrument_class if type(cls) is type else meta.install_settings)(names)(cls)
    return decorate


def reflect(obj):
    scalar.reflected(obj)
    return meta.namespace(obj, "all")


def measure(root, ids, filename, predicates=False):
    os.chdir(root)
    sys.path.insert(0, str(root))
    profile = base.profile = base.Profile(root, ids)
    if predicates:
        import profile_predicates as pred
        base.Transform = pred.Transform
        builtins._rwx_predicate_enter = pred.enter
    else:
        base.Transform = Transform
    sys.meta_path.insert(0, base.Finder())
    builtins._rwx_scalars = install
    builtins._rwx_cold_attr = profile.attribute
    builtins._rwx_reflected = reflect
    builtins._rwx_ns = meta.namespace
    # Same standard-library setup and imports as a cold test-file invocation.
    profile.mask = (1 << len(ids)) - 1
    module = importlib.import_module(filename[:-3].replace("/", "."))
    for clsname in sorted({parts[1] for test in ids if len(parts := test.split("::")) == 3}):
        cls = getattr(module, clsname)
        setup = getattr(cls, "setUpClass", None)
        if not setup or not profile.relative(setup.__func__.__code__.co_filename):
            continue
        profile.mask = sum(1 << i for i,test in enumerate(ids) if test.split("::")[1] == clsname)
        profile.seen.clear()
        setup()
        if teardown := getattr(cls, "tearDownClass", None):
            teardown()
    ignored = {"registered", "installed"}
    return {site: {k: v for k,v in profile.data[i].items() if k not in ignored}
            for site,i in profile.sites.items() if any(k not in ignored for k in profile.data[i])}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for arg in ("root","catalog","output"):
        p.add_argument(arg, type=Path)
    p.add_argument("--file")
    p.add_argument("--predicates",action="store_true")
    p.add_argument("--workers", type=int, default=8)
    a = p.parse_args()
    root, catalog, out = [q.resolve() for q in (a.root,a.catalog,a.output)]
    groups = defaultdict(list)
    for t in json.loads(catalog.read_bytes())["tests"]:
        groups[t["file"]].append(t["id"])
    if a.file:
        out.write_text(json.dumps(measure(root,sorted(groups[a.file]),a.file,a.predicates)))
        return
    ids = sorted(n for names in groups.values() for n in names)
    indices = {n:i for i,n in enumerate(ids)}
    def work(file):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)/"coverage.json"
            subprocess.run([sys.executable,"-B",__file__,str(root),str(catalog),str(data),"--file",file]
                           + (["--predicates"] if a.predicates else []),check=True)
            return sorted(groups[file]), json.loads(data.read_bytes())
    facts = defaultdict(dict)
    with ThreadPoolExecutor(a.workers) as pool:
        for members, observed in pool.map(work,groups):
            translation = {}
            for site, entries in observed.items():
                info = facts["cold:"+site]
                for name, mask in entries.items():
                    if mask not in translation:
                        translation[mask] = sum(1 << indices[n] for i,n in enumerate(members) if mask & (1 << i))
                    info[name] = info.get(name,0) | translation[mask]
    values = {}
    packed = {site:{k:values.setdefault(v,len(values)) for k,v in entries.items()} for site,entries in facts.items()}
    if a.predicates:
        packed["cold:predicate-catalog"] = {"complete":values.setdefault((1 << len(ids))-1,len(values))}
    with gzip.open(out,"wt") as f:
        json.dump({"ids":ids,"facts":packed,"masks":[hex(i)[2:] for i in values],"unverified":[]},f,separators=(",",":"))
    print(f"Traced {len(groups)} independent imports and class setups for {len(ids)} tests",flush=True)


if __name__ == "__main__":
    main()
