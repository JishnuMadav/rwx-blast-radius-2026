"""Align changed statements to the declarations that actually own them."""
import ast
from difflib import SequenceMatcher


def statement_key(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return type(node).__name__, node.name
    target = node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else getattr(node, "target", None)
    if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(target, ast.Name):
        return "binding", target.id
    return "statement", ast.dump(node)


def aligned(a, b, old=(), new=()):
    """Yield old/new AST paths and explicit statement gaps instead of shifting indices."""
    if type(a) is not type(b):
        yield old, new, None
    elif isinstance(a, ast.Dict) and len(a.keys) != len(b.keys):
        yield old, new, None
    elif isinstance(a, ast.AST):
        for field in a._fields:
            yield from aligned(getattr(a, field), getattr(b, field), old + (field,), new + (field,))
    elif isinstance(a, list):
        if len(a) == len(b):
            for i, (x, y) in enumerate(zip(a, b)):
                yield from aligned(x, y, old + (i,), new + (i,))
        elif (old and old[-1] in ("body", "orelse", "finalbody")
              and all(isinstance(n, ast.stmt) for seq in (a, b) for n in seq)):
            left, right = [statement_key(n) for n in a], [statement_key(n) for n in b]
            matcher = SequenceMatcher(None, left, right, autojunk=False)
            for tag, i, j, k, l in matcher.get_opcodes():
                if tag == "equal":
                    for n, m in zip(range(i, j), range(k, l)):
                        if left[n][0] != "statement":
                            yield from aligned(a[n], b[m], old + (n,), new + (m,))
                elif tag == "replace" and j - i == l - k:
                    for n, m in zip(range(i, j), range(k, l)):
                        yield from aligned(a[n], b[m], old + (n,), new + (m,))
                else:
                    yield old, new, (i, j, k, l)
        else:
            yield old, new, None
    elif a != b:
        yield old, new, None


def safe_definition(node):
    if isinstance(node, ast.Lambda):
        return all(isinstance(v, ast.Constant) for v in node.args.defaults + [d for d in node.args.kw_defaults if d is not None])
    try:
        ast.literal_eval(node)
        return True
    except (ValueError, TypeError, SyntaxError):
        return False


def changed_dispatch_keys(before, after):
    """Return changed keys for pure table edits, preserving precedence around unpacking."""
    def describe(table):
        expansions, items = [], {}
        for key, value in zip(table.keys, table.values):
            if key is None:
                expansions.append(ast.dump(value))
            elif isinstance(key, ast.Constant) and type(key.value) is str and key.value not in items:
                items[key.value] = (len(expansions), value)
            else:
                return None
        return expansions, items
    left, right = describe(before), describe(after)
    if left is None or right is None or left[0] != right[0]:
        return None
    changed = set()
    for key in left[1].keys() | right[1].keys():
        a, b = left[1].get(key), right[1].get(key)
        if a is not None and b is not None and a[0] == b[0] and ast.dump(a[1]) == ast.dump(b[1]):
            continue
        if any(item is not None and not safe_definition(item[1]) for item in (a, b)):
            return None
        changed.add(key)
    return changed
