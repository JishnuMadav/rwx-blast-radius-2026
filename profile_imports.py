"""Record module dependencies for each catalog test in a fresh interpreter state."""
import argparse
import concurrent.futures
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile

import pytest
from build_map import MeasuredPool


class Imports:
    def __init__(self):
        self.names = set()
        self.passed = False
        self.failed = False

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "sqlglot" or fullname.startswith("sqlglot."):
            self.names.add(fullname)
        return None

    def pytest_runtest_logreport(self, report):
        self.failed |= report.failed or report.skipped
        self.passed |= report.when == "call" and report.passed


def measure(job):
    number, test = job
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "imports.json"
        pid = os.fork()
        if pid == 0:
            try:
                with open(os.devnull, "w") as sink:
                    os.dup2(sink.fileno(), 1)
                    os.dup2(sink.fileno(), 2)
                probe = Imports()
                sys.meta_path.insert(0, probe)
                concurrent.futures.ProcessPoolExecutor = MeasuredPool
                status = pytest.main(["-qq", "-p", "no:cacheprovider", test], plugins=[probe])
                if status == 0 and probe.passed and not probe.failed:
                    dest.write_text(json.dumps(sorted(probe.names)))
                os._exit(0)
            except BaseException:
                os._exit(1)
        os.waitpid(pid, 0)
        return number, json.loads(dest.read_bytes()) if dest.exists() else None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for field in ("source", "catalog", "output"):
        p.add_argument(field, type=Path)
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args()
    root, catalog, out = (x.resolve() for x in (args.source, args.catalog, args.output))
    ids = sorted(t["id"] for t in json.loads(catalog.read_bytes())["tests"])
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    if any(n.split(".", 1)[0] in {"sqlglot", "tests"} for n in sys.modules):
        raise RuntimeError("The import template has already loaded a target module")
    import multiprocessing
    masks = {}
    missing = []
    with concurrent.futures.ProcessPoolExecutor(args.workers, mp_context=multiprocessing.get_context("fork")) as pool:
        for index, files in pool.map(measure, enumerate(ids), chunksize=2):
            if files is None:
                missing.append(ids[index])
            else:
                for name in files:
                    masks[name] = masks.get(name, 0) | (1 << index)
    with gzip.open(out, "wt") as f:
        json.dump({"ids": ids, "unverified": missing, "modules": {k: format(v, "x") for k, v in masks.items()}}, f)
    print(f"Verified {len(ids) - len(missing)}/{len(ids)} isolated baseline tests.")


if __name__ == "__main__":
    main()
