import ast
import unittest
from pathlib import Path

from selector import Dependencies
from structure import aligned, changed_dispatch_keys
from witness import Evidence, ancestors, apply_hunks, differences, inserted_raise, read_patch, semantic_lines


class WitnessTests(unittest.TestCase):
    def test_regrouped_predicates_retain_cold_and_lazy_readers(self):
        before = ast.parse("def f(a,b,c):\n return a and b and c")
        after = ast.parse("def f(a,b,c):\n return a and b or c")
        a,b=before.body[0].body[0].value,after.body[0].body[0].value
        ids=["unused","reached"]
        dep=Dependencies({"ids":ids,"files":{},"masks":[],"function_ranges":{},"unverified":[]},
                         [{"id":name,"number":i} for i,name in enumerate(ids)])
        site=f"predicate:m.py:{a.lineno}:{a.col_offset}:BoolOp:{a.end_lineno}:{a.end_col_offset}"
        facts={"cold:predicate-catalog":{"complete":0},site:{"registered":0,"ran":1}}
        ev=Evidence(Path("."),{"ids":ids,"facts":facts,"masks":["3","2","4"]},dep)
        self.assertEqual(ev.predicate_reach("m.py",[before.body[0],a],[after.body[0],b]),2)
        other=ast.parse("a and b or d",mode="eval").body
        self.assertIsNone(ev.predicate_reach("m.py",[a],[other]))
        facts["cold:"+site]={"ran":1}
        self.assertIsNone(ev.predicate_reach("m.py",[a],[b]))
        del facts["cold:"+site]
        facts[site]["ran"]=2
        self.assertIsNone(ev.predicate_reach("m.py",[a],[b]))
    def test_deleting_a_method_does_not_reassign_the_next_delta(self):
        left = ast.parse("class C:\n def old(self): return 0\n def stay(self): return 1")
        right = ast.parse("class C:\n def stay(self): return 2")
        items = list(aligned(left, right))
        self.assertEqual([gap for _,_,gap in items if gap], [(0,1,0,0)])
        oldpath,newpath,_ = next(x for x in items if x[2] is None)
        self.assertEqual(ancestors(left,oldpath)[-1], 1)
        self.assertEqual(ancestors(right,newpath)[-1], 2)

    def test_table_edits_keep_unpack_order_and_opaque_evaluation(self):
        table = lambda s: ast.parse(s,mode="eval").body
        self.assertEqual(changed_dispatch_keys(table("{**base, 'a': 1}"),table("{'a': 1, **base}")),{"a"})
        self.assertIsNone(changed_dispatch_keys(table("{**a, 'x': 0}"),table("{**b, 'x': 0}")))
        self.assertIsNone(changed_dispatch_keys(table("{}"),table("{'x': make()}")))

    def test_binding_private_to_deletion_requires_all_uses_inside(self):
        f = ast.parse("def f(x):\n if x:\n  tmp = 1\n return 0").body[0]
        self.assertTrue(Evidence.private_to_gap(f,[f.body[0]],{"tmp"}))
        f.body[-1] = ast.parse("return tmp").body[0]
        self.assertFalse(Evidence.private_to_gap(f,[f.body[0]],{"tmp"}))
        f.body[-1] = ast.parse("return locals()").body[0]
        self.assertFalse(Evidence.private_to_gap(f,[f.body[0]],{"tmp"}))

    def test_generator_boundary_includes_uniterated_callers(self):
        first = ast.parse("def f():\n yield 1\n return")
        second = ast.parse("def f():\n return")
        ids=["iterated","uniterated"]
        dep=Dependencies({"ids":ids,"files":{},"masks":[],"function_ranges":{},"unverified":[]},
                         [{"id":n,"number":i} for i,n in enumerate(ids)])
        ev=Evidence(Path("."),{"ids":ids,"facts":{},"masks":[]},dep)
        a,b,gap=next(aligned(first,second))
        self.assertEqual(ev.statement_gap("sqlglot/example.py",ancestors(first,a),ancestors(second,b),gap),{0,1})
        outer=ast.parse("def f():\n def g():\n  yield 1\n return g").body[0]
        self.assertFalse(Evidence.generator(outer))

    def test_unknown_startup_origin_keeps_fallback(self):
        ids=["a","b"]
        dep=Dependencies({"ids":ids,"files":{},"masks":[],"function_ranges":{},"unverified":[]},
                         [{"id":n,"number":i} for i,n in enumerate(ids)])
        ev=Evidence(Path("."),{"ids":ids,"facts":{"attr:A:F":{"read":0},"cold:attr:A:F":{"read":1},
                    "origin:attr:A:F":{"read|unknown":0}},"masks":["4","2"]},dep,
                    imports={"ids":ids,"modules":{"sqlglot.a":"2"},"unverified":[]})
        self.assertEqual(ev.setting_reads("attr:A:F","read"),4)

    def test_boolean_map_proof_requires_an_isolated_code_edit(self):
        before=ast.parse("class C:\n    fields = {'x': True, 'y': False}")
        after=ast.parse("class C:\n    fields = {'x': False, 'y': False}")
        ev_ids=["good","missing","reflects"]
        dep=Dependencies({"ids":ev_ids,"files":{},"masks":[],"function_ranges":{},"unverified":[]},
                         [{"id":n,"number":i} for i,n in enumerate(ev_ids)])
        v=before.body[0].body[0].value
        ordinary=f"demo.py:{v.lineno}:{v.col_offset}:mapping"
        checked=f"validator:demo.py:{v.lineno}:{v.col_offset}"
        ev=Evidence(Path("."),{"ids":ev_ids,"masks":["1","2","4","7"],
                    "facts":{ordinary:{"registered":0,"all":3},checked:{"validated":0,"invalid:x":1,"all":2}}},dep)
        delta,=differences(before,after)
        self.assertEqual(ev.masks_for_change("demo.py",before,after,delta,True),6)
        self.assertEqual(ev.masks_for_change("demo.py",before,after,delta,False),7)

    def test_lazy_import_in_shared_registry_is_recognized(self):
        fn=ast.parse("def get(key):\n if key not in registry:\n  registry[key]=importlib.import_module(key)\n return registry[key]").body[0]
        self.assertTrue(Evidence.loads_modules(fn))

    def test_import_shortcut_requires_one_unconditional_new_raise(self):
        before = ast.parse("x = 1")
        self.assertTrue(inserted_raise(before, ast.parse("x = 1\nraise ValueError()")))
        self.assertFalse(inserted_raise(before, ast.parse("x = 2\nraise ValueError()")))
        self.assertFalse(inserted_raise(before, ast.parse("x = 1\nif x:\n    raise ValueError()")))

    def test_private_list_writes_keep_live_consumers(self):
        old = ast.parse("class C:\n    FLAG = False")
        new = ast.parse("class C:\n    FLAG = True")
        ids = list("abcd")
        dep = Dependencies({"ids": ids, "masks": [], "files": {}, "function_ranges": {}, "unverified": []},
                           [{"id": n, "number": i} for i, n in enumerate(ids)])
        facts = {"guard:demo.C:FLAG": {"installed": 0, "analyzed": 1, "append": 2, "read": 3},
                 "consumer:FLAG": {"possible": 4}, "attr:demo.C:FLAG": {"read": 1}}
        ev = Evidence(Path("."), {"ids": ids, "masks": ["10", "f", "3", "4", "a"], "facts": facts}, dep)
        delta, = differences(old, new)
        self.assertEqual(ev.masks_for_change("demo.py", old, new, delta, isolated=True), 6)
        self.assertEqual(ev.masks_for_change("demo.py", old, new, delta, isolated=False), 15)

    def test_shifted_type_ignore_does_not_invalidate_module(self):
        source = "import sys\n\ndef f(x):\n    return x == 1  # type: ignore[comparison-overlap]\n"
        before = ast.parse(source)
        after = ast.parse("# moved down\n" + source.replace("== 1", "== 2"))
        delta, = differences(before, after)
        self.assertEqual(semantic_lines(ancestors(before, delta), {1, 4}), {4})

    def test_key_rename_keeps_both_names_and_whole_table_readers(self):
        before = ast.parse("class Demo:\n    MAP = {'old': lambda: 1}")
        after = ast.parse("class Demo:\n    MAP = {'new': lambda: 1}")
        catalog = list("abcd")
        dep = Dependencies({"ids": catalog, "masks": [], "files": {}, "function_ranges": {}, "unverified": []},
                           [{"id": n, "number": i} for i, n in enumerate(catalog)])
        site = {"registered": 0, "key:old": 1, "key:new": 2, "all": 3, "key:other": 4}
        ev = Evidence(Path("."), {"ids": catalog, "masks": ["10", "1", "2", "4", "8"],
                                  "facts": {"demo.py:2:10:dispatch": site}}, dep)
        delta, = differences(before, after)
        self.assertEqual(ev.masks_for_change("demo.py", before, after, delta), 7)

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
