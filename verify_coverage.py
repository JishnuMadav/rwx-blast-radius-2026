"""Remeasure unresolved baseline tests using preallocated line buffers."""
import argparse
import gzip
import json
import os
from pathlib import Path
import sys

import pytest


class BufferedCoverage:
    def __init__(self, root, test):
        self.test, self.active, self.passed, self.failed = test, False, False, False
        self.buffers = {}
        self.relative = {}
        self.fixtures = set()
        self.root = str(root)
        for directory in ("sqlglot", "tests"):
            for f in (root / directory).rglob("*.py"):
                self.buffers[str(f)] = bytearray(f.read_bytes().count(b"\n") + 3)
                self.relative[str(f)] = f.relative_to(root).as_posix()

    def line(self, code, n):
        if self.active:
            buf = self.buffers.get(code.co_filename)
            if buf is not None and n < len(buf):
                buf[n] = 1
        return sys.monitoring.DISABLE

    def audit(self, event, args):
        if self.active and event == "open" and isinstance(args[0], str):
            full = os.path.abspath(args[0])
            prefix = self.root + os.sep
            if full.startswith(prefix):
                rel = full[len(prefix):].replace(os.sep, "/")
                if rel.startswith("tests/fixtures/"):
                    self.fixtures.add(rel)

    def pytest_runtest_logstart(self, nodeid, location):
        self.active = True
        sys.monitoring.restart_events()

    def pytest_runtest_logreport(self, report):
        self.failed |= report.failed
        self.passed |= report.when == "call" and report.passed

    def pytest_runtest_logfinish(self, nodeid, location):
        self.active = False


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "map", "output"):
        p.add_argument(name, type=Path)
    a = p.parse_args()
    root, dest = a.source.resolve(), a.output.resolve()
    data = json.loads(gzip.decompress(a.map.read_bytes()))
    masks = [int(v, 16) for v in data["masks"]]
    indices = {m: i for i, m in enumerate(masks)}
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    mon = sys.monitoring
    for test in list(data["unverified"]):
        r = BufferedCoverage(root, test)
        mon.use_tool_id(mon.COVERAGE_ID, "line buffers")
        mon.register_callback(mon.COVERAGE_ID, mon.events.LINE, r.line)
        mon.set_events(mon.COVERAGE_ID, mon.events.LINE)
        sys.addaudithook(r.audit)
        try:
            status = pytest.main(["-q", "--disable-warnings", test], plugins=[r])
        finally:
            r.active = False
            mon.set_events(mon.COVERAGE_ID, 0)
            mon.free_tool_id(mon.COVERAGE_ID)
        if status != 0 or not r.passed or r.failed:
            continue
        bit = 1 << data["ids"].index(test)
        def merge(rel, line):
            lines = data["files"].setdefault(rel, {})
            old = masks[lines[line]] if line in lines else 0
            value = old | bit
            if value not in indices:
                indices[value] = len(masks)
                masks.append(value)
            lines[line] = indices[value]
        for f, buf in r.buffers.items():
            for n, hit in enumerate(buf):
                if hit:
                    merge(r.relative[f], str(n))
        for path in r.fixtures:
            merge(path, "0")
        data["unverified"].remove(test)
    data["masks"] = [format(m, "x") for m in masks]
    with gzip.open(dest, "wt", encoding="utf8") as f:
        json.dump(data, f, separators=(",", ":"))
    print("Unresolved baseline tests:", len(data["unverified"]))


if __name__ == "__main__":
    main()
