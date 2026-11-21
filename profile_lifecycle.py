"""Measure cold test-file imports and shared unittest setup in isolated interpreters."""
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import gzip
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def measure(root, file, ids):
    os.chdir(root)
    sys.path.insert(0, str(root))
    hits = defaultdict(set)
    @lru_cache(None)
    def relative(filename):
        if not isinstance(filename, str) or filename.startswith("<"):
            return ""
        path = Path(os.path.abspath(filename))
        return path.relative_to(root).as_posix() if path.is_relative_to(root) else ""
    def line(code, n):
        rel = relative(code.co_filename)
        if rel.startswith(("sqlglot/", "tests/")):
            hits[rel].add(n)
        return sys.monitoring.DISABLE
    def audit(event, args):
        if event == "open":
            rel = relative(args[0])
            if rel.startswith("tests/fixtures/"):
                hits[rel].add(0)
    def snapshot():
        return {path: sorted(lines) for path, lines in hits.items()}
    sys.addaudithook(audit)
    mon = sys.monitoring
    mon.use_tool_id(mon.COVERAGE_ID, "cold import and class setup")
    mon.register_callback(mon.COVERAGE_ID, mon.events.LINE, line)
    mon.set_events(mon.COVERAGE_ID, mon.events.LINE)
    module = importlib.import_module(file[:-3].replace("/", "."))
    output = [(ids, snapshot())]
    classes = defaultdict(list)
    for test in ids:
        parts = test.split("::")
        if len(parts) == 3:
            classes[parts[1]].append(test)
    for name, members in classes.items():
        cls = getattr(module, name)
        setup = getattr(cls, "setUpClass", None)
        if not setup or not relative(setup.__func__.__code__.co_filename):
            continue
        hits.clear()
        mon.restart_events()
        setup()
        output.append((members, snapshot()))
        if hasattr(cls, "tearDownClass"):
            cls.tearDownClass()
    mon.set_events(mon.COVERAGE_ID, 0)
    return output


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("source", "catalog", "output"):
        p.add_argument(key, type=Path)
    p.add_argument("--file")
    p.add_argument("--workers", type=int, default=6)
    a = p.parse_args()
    root, catalog, dest = (path.resolve() for path in (a.source, a.catalog, a.output))
    tests = json.loads(catalog.read_bytes())["tests"]
    groups = defaultdict(list)
    for row in tests:
        groups[row["file"]].append(row["id"])
    if a.file:
        dest.write_text(json.dumps(measure(root, a.file, groups[a.file])))
        return
    ids = sorted(row["id"] for row in tests)
    indices = {name: i for i, name in enumerate(ids)}
    total = defaultdict(dict)
    def one(file):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "measure.json"
            subprocess.run([sys.executable, "-B", __file__, str(root), str(catalog),
                            str(output), "--file", file], check=True)
            return json.loads(output.read_bytes())
    with ThreadPoolExecutor(a.workers) as pool:
        for results in pool.map(one, groups):
            for members, files in results:
                mask = sum(1 << indices[name] for name in members)
                for file, lines in files.items():
                    covered = total[file]
                    for n in lines:
                        covered[n] = covered.get(n, 0) | mask
    table = {}
    packed = {file: {n: table.setdefault(mask, len(table)) for n, mask in lines.items()}
              for file, lines in total.items()}
    with gzip.open(dest, "wt", encoding="utf8") as output:
        json.dump({"ids": ids, "files": packed, "masks": [format(mask, "x") for mask in table]},
                  output, separators=(",", ":"))
    print(f"Measured {len(groups)} test modules, {len(ids)} runnable tests and {len(packed)} dependencies")


if __name__ == "__main__":
    main()
