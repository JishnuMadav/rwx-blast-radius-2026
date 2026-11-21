import ast
import unittest
from pathlib import Path

from selector import Dependencies
from witness import Evidence, apply_hunks, differences, read_patch


class WitnessTests(unittest.TestCase):
    def test_lifecycle_mask_uses_live_catalog_numbers(self):
        model = {"ids": ["test_a", "test_b"], "masks": [], "unverified": [], "files": {}, "function_ranges": {}}
        deps = Dependencies(model, [{"id": "test_a", "number": 17}, {"id": "test_b", "number": 3}])
        phases = {"ids": deps.ids, "files": {"fixture.sql": {"0": 0}, "module.py": {"19": 1}},
                  "masks": ["3", "2"]}
        evidence = Evidence(Path("."), {"ids": deps.ids, "masks": [], "facts": {}}, deps, phases)
        self.assertEqual(evidence.lifecycle_hits("fixture.sql", [0]), {3, 17})
        self.assertEqual(evidence.lifecycle_hits("module.py", [18, 19]), {3})

    def test_integer_boundary_uses_observed_operand(self):
        before = ast.parse("def f(x):\n    return x < 2")
        after = ast.parse("def f(x):\n    return x <= 2")
        model = {"ids": ["below", "at"], "masks": [], "unverified": [], "files": {}, "function_ranges": {}}
        deps = Dependencies(model, [{"id": name, "number": i} for i, name in enumerate(model["ids"])])
        facts = {"ids": deps.ids, "masks": ["1", "2"],
                 "facts": {"number:demo.py:2:11": {"1": 0, "2": 1}}}
        ev = Evidence(Path("."), facts, deps)
        delta, = differences(before, after)
        self.assertEqual(ev.masks_for_change("demo.py", before, after, delta), 2)

    def test_reconstruct_multiple_hunks(self):
        patch = "--- a/t.py\n+++ b/t.py\n@@ -0,0 +1 @@\n+# first\n@@ -2 +3 @@\n-b\n+B"
        self.assertEqual(apply_hunks("a\nb\nc\n", read_patch(patch)["t.py"]), "# first\na\nB\nc\n")

    def test_refuse_incomplete_or_unsupported_patches(self):
        valid = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\n"
        for patch in (valid + "diff --git a/t b/t", valid.replace("@@ -1", "@@ -1,3"),
                      valid.replace("+++ b/a", "+++ b/b"), valid.replace("a/a", "a/../a"),
                      "diff --git a/t b/t\nGIT binary patch\n"):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                read_patch(patch)
        with self.assertRaises(ValueError):
            apply_hunks("wrong", read_patch(valid)["a"])

    def test_value_change_uses_key_specific_coverage(self):
        before = ast.parse("class Demo:\n    MAP = {'x': (1, 2), 'y': (3, 4)}")
        after = ast.parse("class Demo:\n    MAP = {'x': (1, 9), 'y': (3, 4)}")
        model = {"ids": ["test_x", "test_y"], "masks": [], "unverified": [], "files": {}, "function_ranges": {}}
        dep = Dependencies(model, [{"id": name, "number": i} for i, name in enumerate(model["ids"])])
        profile = {"ids": dep.ids, "masks": ["4", "1", "2"], "facts": {
            "demo.py:2:10:mapping": {"registered": 0, "v:'x'": 1, "v:'y'": 2}}}
        evidence = Evidence(Path("."), profile, dep)
        delta, = differences(before, after)
        self.assertEqual(evidence.masks_for_change("demo.py", before, after, delta), 1)


if __name__ == "__main__":
    unittest.main()
