"""Measure baseline test dependencies on the pinned SQLGlot tree with Python 3.12+."""
import argparse
import ast
from collections import defaultdict
import concurrent.futures
from functools import lru_cache
import gzip
import json
import os
from pathlib import Path
import pickle
import sys

import pytest


def function_ranges(source):
    """Identify baseline lines executed inside functions rather than during import."""
    tree = ast.parse(source)
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = max(node.lineno + 1, node.body[0].lineno)
            if start <= node.end_lineno:
                spans.append((start, node.end_lineno))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


class MeasuredPool(concurrent.futures.Executor):
    """Replay submitted calls in the recording process, including pickle hooks."""
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer:
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            call, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            result = call(*args, **kwargs)
            future.set_result(pickle.loads(pickle.dumps(result)))
        except Exception as exc:
            future.set_exception(exc)
        return future


class Recorder:
    def __init__(self, root, ids):
        self.root = root
        self.ids = ids
        self.indices = {name: i for i, name in enumerate(ids)}
        self.covered = defaultdict(int)
        self.mask = 0
        self.passed = set()
        self.failed = set()

    @lru_cache(maxsize=None)
    def relative(self, filename):
        if not isinstance(filename, str) or filename.startswith("<"):
            return None
        path = Path(os.path.abspath(filename))
        if path.is_relative_to(self.root):
            return path.relative_to(self.root).as_posix()
        return None

    def line(self, code, lineno):
        path = self.relative(code.co_filename)
        if self.mask and path and path.startswith(("sqlglot/", "tests/")):
            self.covered[path, lineno] |= self.mask
        return sys.monitoring.DISABLE

    def audit(self, event, args):
        if event == "open" and self.mask:
            path = self.relative(args[0])
            if path and path.startswith("tests/fixtures/"):
                self.covered[path, 0] |= self.mask

    def pytest_collection_modifyitems(self, items):
        items[:] = [item for item in items if item.nodeid in self.indices]

    def pytest_runtest_logstart(self, nodeid, location):
        self.mask = 1 << self.indices[nodeid]
        sys.monitoring.restart_events()

    def pytest_runtest_logreport(self, report):
        if report.when == "call" and report.passed and report.nodeid not in self.failed:
            self.passed.add(report.nodeid)
        if report.failed:
            self.failed.add(report.nodeid)
            self.passed.discard(report.nodeid)

    def pytest_runtest_logfinish(self, nodeid, location):
        self.mask = 0

    def save(self, out):
        bitsets = {}
        files = defaultdict(dict)
        for (path, line), mask in sorted(self.covered.items()):
            files[path][line] = bitsets.setdefault(mask, len(bitsets))
        data = {"ids": self.ids, "unverified": sorted(set(self.ids) - self.passed),
                "masks": [format(m, "x") for m in bitsets], "files": files}
        data["function_ranges"] = {p: function_ranges((self.root / p).read_bytes())
                                   for p in files if p.endswith(".py")}
        temporary = out.with_suffix(".tmp")
        with gzip.open(temporary, "wt", encoding="utf8") as f:
            json.dump(data, f, separators=(",", ":"))
        temporary.replace(out)
        print(f"Verified {len(self.passed)}/{len(self.ids)} baseline tests; indexed {len(files)} files.")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source", type=Path)
    p.add_argument("catalog", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--augment-workers", action="store_true",
                   help="Extend an existing baseline map with a serialized worker replay")
    args = p.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    ids = sorted(t["id"] for t in json.loads(args.catalog.read_bytes())["tests"])
    recorder = Recorder(root, ids)
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    files = ["tests"]
    if args.augment_workers:
        with gzip.open(output, "rt", encoding="utf8") as f:
            prior = json.load(f)
        if prior["ids"] != ids:
            raise ValueError("The baseline and supplemental catalogs differ")
        masks = [int(h, 16) for h in prior["masks"]]
        for path, lines in prior["files"].items():
            for line, index in lines.items():
                recorder.covered[path, int(line)] = masks[index]
        recorder.passed.update(set(ids) - set(prior["unverified"]))
        recorder.failed.update(prior["unverified"])
        files = sorted({n.split("::")[0] for n in ids
                        if (root / n.split("::")[0]).is_file()})
        files = [p for p in files if "ProcessPoolExecutor" in (root / p).read_text(encoding="utf8")]
        if not files:
            raise ValueError("No process pool tests found")
        concurrent.futures.ProcessPoolExecutor = MeasuredPool
    sys.addaudithook(recorder.audit)
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    mon.use_tool_id(tool, "rwx-test-coverage")
    mon.register_callback(tool, mon.events.LINE, recorder.line)
    mon.set_events(tool, mon.events.LINE)
    try:
        status = pytest.main(["-q", "--disable-warnings", "--continue-on-collection-errors", *files], plugins=[recorder])
    finally:
        mon.set_events(tool, 0)
        mon.free_tool_id(tool)
    if status not in (0, 1) or not recorder.passed:
        raise SystemExit(f"Unusable baseline test run ({status})")
    recorder.save(output)


if __name__ == "__main__":
    main()
