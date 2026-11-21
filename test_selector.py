import unittest

from selector import Dependencies, changed_lines, schedule


class SelectorTests(unittest.TestCase):
    def test_hunks_use_baseline_line_numbers(self):
        diff = "--- a/a.py\n+++ b/a.py\n@@ -7,2 +7,3 @@\n keep\n-old\n+new\n+new2\n@@ -20,0 +22,1 @@\n+insert\n"
        self.assertEqual(changed_lines(diff), {"a.py": {8, 20, 21}})

    def test_catalog_numbers_can_change(self):
        model = {"ids": ["test_a", "test_b"], "unverified": [], "masks": ["1"],
                 "files": {"a.py": {"3": 0, "4": 0}}, "function_ranges": {"a.py": [[4, 6]]}}
        catalog = [{"id": "test_a", "number": 9}, {"id": "test_b", "number": 1}, {"id": "test_c", "number": 5}]
        deps = Dependencies(model, catalog)
        self.assertEqual(deps.select("--- a/a.py\n+++ b/a.py\n@@ -4 +4 @@\n-x\n+y"), {5, 9})
        self.assertEqual(deps.select("--- a/b.py\n+++ b/b.py\n@@ -4 +4 @@\n-x\n+y"), {1, 5, 9})
        self.assertEqual(deps.select("--- a/a.py\n+++ b/a.py\n@@ -3 +3 @@\n-X=1\n+X=2"), {1, 5, 9})

    def test_worker_startup_cost(self):
        self.assertEqual(len(schedule([1, 2], {1: 30, 2: 90})), 1)
        self.assertEqual(len(schedule([1, 2], {1: 8000, 2: 8000})), 2)
        self.assertEqual(schedule([], {}), [])


if __name__ == "__main__":
    unittest.main()
