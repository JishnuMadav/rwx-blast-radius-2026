"""Select tests from baseline execution evidence and balance their recorded runtimes."""
import argparse
import gzip
import json
from pathlib import Path
import re


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def changed_lines(diff):
    """Return old-file line numbers. Pure insertions have two adjacent anchors."""
    result = {}
    path = None
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = None
            in_hunk = False
        elif not in_hunk and line.startswith("--- a/"):
            path = line[6:].split("\t", 1)[0]
            result.setdefault(path, set())
        elif line.startswith("@@ "):
            match = HUNK.match(line)
            if path is None or match is None:
                return None
            old_line = int(match[1]) + (match[2] == "0")
            in_hunk = True
            removed = False
        elif in_hunk:
            if line.startswith(" "):
                old_line += 1
                removed = False
            elif line.startswith("-"):
                result[path].add(old_line)
                old_line += 1
                removed = True
            elif line.startswith("+") and not removed:
                result[path].update([max(1, old_line - 1), old_line])
        elif line.startswith(("new file mode", "deleted file mode", "rename from ", "copy from ", "Binary files ")):
            return None
    return result or None


class Dependencies:
    def __init__(self, coverage, tests):
        self.ids = coverage["ids"]
        self.masks = [int(s, 16) for s in coverage["masks"]]
        self.files = coverage["files"]
        self.ranges = coverage["function_ranges"]
        self.number = {test["id"]: test["number"] for test in tests}
        self.all_tests = set(self.number.values())
        self.unverified = {self.number[name] for name in coverage["unverified"] if name in self.number}
        self.unverified.update(self.number[name] for name in self.number.keys() - set(self.ids))

    def select(self, diff):
        changes = changed_lines(diff)
        return self.select_lines(changes)

    def select_lines(self, changes):
        if not changes:
            return self.all_tests
        mask = 0
        for path, lines in changes.items():
            index = self.files.get(path)
            if not index or not lines:
                return self.all_tests
            if "0" in index:
                mask |= self.masks[index["0"]]
                continue
            for line in lines:
                # A class constant or module expression may run only once during
                # import even though its value changes behavior in many tests.
                if path.endswith(".py") and not any(a <= line <= b for a, b in self.ranges.get(path, ())):
                    return self.all_tests
                hit = index.get(str(line))
                if hit is None:
                    return self.all_tests
                mask |= self.masks[hit]
        return self.unverified | self.numbers(mask)

    def numbers(self, mask):
        chosen = set()
        while mask:
            bit = mask & -mask
            number = self.number.get(self.ids[bit.bit_length() - 1])
            if number is not None:
                chosen.add(number)
            mask ^= bit
        return chosen


def schedule(selected, costs):
    if not selected:
        return []
    ordered = sorted(selected, key=lambda n: (-costs[n], n))
    best = None
    best_cost = float("inf")
    for count in range(1, min(8, len(ordered)) + 1):
        if 2000 * count + costs[ordered[0]] >= best_cost:
            break
        workers = [[] for _ in range(count)]
        loads = [0] * count
        for number in ordered:
            worker = loads.index(min(loads))
            workers[worker].append(number)
            loads[worker] += costs[number]
        workers = [ids for ids in workers if ids]
        cost = max(loads) + 2000 * len(workers)
        if cost < best_cost:
            best_cost = cost
            best = workers
    return best


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path("/data"))
    p.add_argument("--map", type=Path, default=Path(__file__).with_name("coverage.json.gz"))
    p.add_argument("--evidence", type=Path, default=Path(__file__).with_name("semantics.json.gz"))
    p.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    p.add_argument("--lifecycle", type=Path, default=Path(__file__).with_name("lifecycle.json.gz"))
    args = p.parse_args()
    tests = json.loads((args.data / "tests.json").read_bytes())["tests"]
    costs = {t["number"]: round(1000 * t["seconds"]) for t in tests}
    with gzip.open(args.map, "rb") as f:
        deps = Dependencies(json.load(f), tests)
    evidence = None
    if args.evidence.is_file() and args.source.is_dir():
        from witness import Evidence
        lifecycle = None
        if args.lifecycle.is_file():
            with gzip.open(args.lifecycle, "rb") as f:
                lifecycle = json.load(f)
        with gzip.open(args.evidence, "rb") as f:
            evidence = Evidence(args.source, json.load(f), deps, lifecycle)
    cache = {}
    for patch in sorted((args.data / "commits").glob("*.patch")):
        diff = patch.read_text(encoding="utf8", errors="replace")
        changes = changed_lines(diff)
        if evidence and changes:
            try:
                selected = evidence.select(diff, changes)
            except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError):
                selected = deps.all_tests
        else:
            selected = deps.select_lines(changes)
        selection = frozenset(selected)
        if selection not in cache:
            cache[selection] = schedule(selection, costs)
        print(json.dumps({"commit": patch.stem, "workers": cache[selection]}, separators=(",", ":")))


if __name__ == "__main__":
    main()
