"""Measure the tests reaching individual Boolean expressions, including comprehensions."""
import ast
import builtins
import sys

import profile_semantics as base


def enter(site, first):
    base.profile.hit(site, "ran")
    return first


class Transform(ast.NodeTransformer):
    def __init__(self, profile, path):
        self.p, self.path = profile, path

    def mark(self, node, first):
        label = f"predicate:{self.path}:{node.lineno}:{node.col_offset}:{type(node).__name__}:{node.end_lineno}:{node.end_col_offset}"
        site = self.p.site(label)
        self.p.hit(site, "registered")
        return base.Transform.wrap(first, "_rwx_predicate_enter", [ast.Constant(site),first])

    wrap = staticmethod(base.Transform.wrap)

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        node.values[0] = self.mark(node,node.values[0])
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        node.left = self.mark(node,node.left)
        return node


if __name__ == "__main__":
    builtins._rwx_predicate_enter = enter
    base.Transform = Transform
    base.Profile.entry = lambda self, code, offset: sys.monitoring.DISABLE
    base.Profile.audit = lambda self, event, args: None
    base.main()
