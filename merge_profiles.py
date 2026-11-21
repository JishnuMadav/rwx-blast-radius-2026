"""Merge observations collected in separate processes against one baseline catalog."""
import argparse
import gzip
import json
from pathlib import Path


def merge(inputs, output):
    facts = {}
    ids = missing = None
    for path in inputs:
        with gzip.open(path, "rt") as stream:
            part = json.load(stream)
        if ids is None:
            ids = part["ids"]
            missing = set(ids)
        if ids != part["ids"]:
            raise ValueError("different catalogs")
        masks = [int(value, 16) for value in part["masks"]]
        missing.intersection_update(part["unverified"])
        for site, values in part["facts"].items():
            current = facts.setdefault(site, {})
            for name, index in values.items():
                current[name] = current.get(name, 0) | masks[index]
    indices = {}
    packed = {site: {key: indices.setdefault(mask, len(indices)) for key, mask in values.items()}
              for site, values in facts.items()}
    with gzip.open(output, "wt", encoding="utf8") as stream:
        json.dump({"ids": ids, "facts": packed, "masks": [format(mask, "x") for mask in indices],
                   "unverified": sorted(missing)}, stream, separators=(",", ":"))
    print(f"Verified {len(ids) - len(missing)} of {len(ids)} tests. Unverified: {sorted(missing)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("inputs", nargs="+", type=Path)
    args = parser.parse_args()
    merge(args.inputs, args.output)
