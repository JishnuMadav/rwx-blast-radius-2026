"""Interpret a patch using source and execution evidence from the pinned revision."""
import ast
from functools import lru_cache
import gzip
import json
from pathlib import Path, PurePosixPath
import re


HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def read_patch(text):
    """Parse standard git hunks, recording both sides and checking their lengths."""
    files = {}
    old = path = hunk = None
    header_seen = False
    counts = [0, 0]
    for line in text.splitlines():
        if hunk is not None and counts != [0, 0]:
            if line == r"\ No newline at end of file":
                continue
            if not line or line[0] not in " +-":
                raise ValueError("truncated hunk")
            prefix, content = line[0], line[1:]
            if prefix != "+":
                hunk[2].append(content)
                counts[0] -= 1
            if prefix != "-":
                hunk[3].append(content)
                counts[1] -= 1
            if min(counts) < 0:
                raise ValueError("invalid hunk length")
            continue
        if line.startswith("diff --git "):
            if header_seen and (not path or not files[path]):
                raise ValueError("missing file body")
            header_seen = True
            old = path = hunk = None
        elif line.startswith("--- "):
            if not line.startswith("--- a/"):
                raise ValueError("non-baseline path")
            old = line[6:]
        elif line.startswith("+++ "):
            if not old or line != "+++ b/" + old:
                raise ValueError("renamed or new file")
            p = PurePosixPath(old)
            if p.is_absolute() or ".." in p.parts or "\\" in old or old in files:
                raise ValueError("unsupported file name")
            path = old
            files[path] = []
        elif line.startswith("@@"):
            m = HEADER.match(line)
            if m is None or path is None:
                raise ValueError("invalid header")
            a, n, b, k = (int(x) if x is not None else 1 for x in m.groups())
            hunk = (a - int(n > 0), b - int(k > 0), [], [])
            files[path].append(hunk)
            counts = [n, k]
        elif line.startswith(("new file mode", "deleted file mode", "rename ", "copy ", "Binary files", "GIT binary", "old mode", "new mode")):
            raise ValueError("non-text change")
    if counts != [0, 0] or not files or not all(files.values()) or (header_seen and not path):
        raise ValueError("incomplete patch")
    return files


def apply_hunks(source, hunks):
    lines = source.splitlines()
    output = []
    cursor = 0
    for old, new, before, after in hunks:
        if old < cursor or old > len(lines) or lines[old:old + len(before)] != before:
            raise ValueError("patch context does not match pinned source")
        output.extend(lines[cursor:old])
        if len(output) != new:
            raise ValueError("inconsistent insertion position")
        output.extend(after)
        cursor = old + len(before)
    output.extend(lines[cursor:])
    return "\n".join(output) + ("\n" if source.endswith("\n") else "")


def tree_key(node):
    return ast.dump(node, include_attributes=False)


def differences(a, b, path=()):
    if type(a) is not type(b):
        yield path
    elif isinstance(a, ast.AST):
        for field in a._fields:
            yield from differences(getattr(a, field), getattr(b, field), path + (field,))
    elif isinstance(a, list):
        if len(a) != len(b):
            yield path
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                yield from differences(x, y, path + (i,))
    elif a != b:
        yield path


def ancestors(tree, path):
    nodes = [tree]
    for key in path:
        tree = tree[key] if isinstance(key, int) else getattr(tree, key)
        nodes.append(tree)
    return nodes


class Evidence:
    def __init__(self, root, data, deps):
        self.root = root
        self.deps = deps
        self.sites = data["facts"]
        if data["ids"] != deps.ids:
            raise ValueError("baseline profile catalogs differ")
        self.masks = [int(value, 16) for value in data["masks"]]
        self.startup = 1 << len(deps.ids)
        self.unverified = {deps.number[n] for n in data.get("unverified", ()) if n in deps.number}

    def fact(self, site, key):
        facts = self.sites.get(site, {})
        return self.masks[facts[key]] if key in facts else 0

    @lru_cache(maxsize=96)
    def source(self, path):
        text = (self.root / path).read_text(encoding="utf-8-sig")
        return text, ast.parse(text, type_comments=True)

    def masks_for_change(self, path, before, after, delta):
        a, b = ancestors(before, delta), ancestors(after, delta)
        for old, new in reversed(list(zip(a, b))):
            if not isinstance(old, ast.Compare) or not isinstance(new, ast.Compare):
                continue
            if (len(old.ops) != 1 or len(new.ops) != 1 or tree_key(old.left) != tree_key(new.left)
                or tree_key(old.comparators[0]) != tree_key(new.comparators[0])):
                continue
            pair = {type(old.ops[0]).__name__, type(new.ops[0]).__name__}
            site = f"{path}:{old.lineno}:{old.col_offset}:cmp"
            if site not in self.sites:
                continue
            if pair in ({"Is", "Eq"}, {"IsNot", "NotEq"}):
                keys = ("?", "01", "10")
            elif pair in ({"Lt", "LtE"}, {"Gt", "GtE"}):
                keys = ("?", "01", "11")
            else:
                continue
            mask = 0
            for key in keys:
                mask |= self.fact(site, key)
            return mask

        classes = [n.name for n in a if isinstance(n, ast.ClassDef)]
        assignments = [(n, m) for i, (n, m) in enumerate(zip(a, b)) if isinstance(n, (ast.Assign, ast.AnnAssign))
                       and type(n) is type(m) and i < len(delta) and delta[i] == "value"]
        if classes and assignments and not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in a):
            assign, updated = assignments[-1]
            target = assign.targets[0] if isinstance(assign, ast.Assign) and len(assign.targets) == 1 else getattr(assign, "target", None)
            if isinstance(target, ast.Name):
                old_val, new_val = assign.value, updated.value
                if isinstance(old_val, ast.Dict) and isinstance(new_val, ast.Dict):
                    for index, expr in enumerate(old_val.values):
                        if any(expr is v for v in a):
                            mapping = f"{path}:{old_val.lineno}:{old_val.col_offset}:mapping"
                            key = old_val.keys[index]
                            if (self.fact(mapping, "registered") and isinstance(key, ast.Constant)
                                and len(old_val.keys) == len(new_val.keys)
                                and all(x is not None and y is not None and tree_key(x) == tree_key(y)
                                        for x, y in zip(old_val.keys, new_val.keys))):
                                try:
                                    ast.literal_eval(new_val.values[index])
                                except (ValueError, TypeError, SyntaxError):
                                    return None
                                return self.fact(mapping, "v:" + repr(key.value)) | self.fact(mapping, "all")
                            site = f"{path}:{expr.lineno}:{expr.col_offset}:callback"
                            if self.fact(site, "registered") and not self.fact(site, "opaque"):
                                replacement = new_val.values[index]
                                if (isinstance(expr, ast.Lambda) and isinstance(replacement, ast.Lambda)
                                    and tree_key(expr.args) == tree_key(replacement.args)):
                                    return self.fact(site, "called")
                                if (isinstance(expr, ast.Call) and isinstance(replacement, ast.Call)
                                    and tree_key(expr.func) == tree_key(replacement.func)
                                    and len(expr.args) == len(replacement.args)
                                    and [k.arg for k in expr.keywords] == [k.arg for k in replacement.keywords]):
                                    inputs = [*expr.args, *(k.value for k in expr.keywords)]
                                    changed = [*replacement.args, *(k.value for k in replacement.keywords)]
                                    if all(tree_key(x) == tree_key(y) or (isinstance(x, ast.Constant)
                                           and isinstance(y, ast.Constant) and type(x.value) is type(y.value))
                                           for x, y in zip(inputs, changed)):
                                        return self.fact(site, "called")
                                return None
                if target.id.isupper() or target.id == "arg_types":
                    try:
                        ast.literal_eval(old_val)
                        ast.literal_eval(new_val)
                    except (ValueError, TypeError, SyntaxError):
                        return None
                    base = path[:-3].replace("/", ".") + "." + ".".join(classes)
                    site = f"attr:{base}:{target.id}"
                    if site in self.sites:
                        return self.fact(site, "read")
        return None

    def ordinary(self, path, before, after, delta, base):
        a, b = ancestors(before, delta), ancestors(after, delta)
        # A boolean precedence change can execute an operand that every
        # baseline test short-circuited. Include entrants to the whole guard.
        expanded = set(base)
        if any(isinstance(n, ast.BoolOp) for n in (*a, *b)):
            stmts = [n for n in a if isinstance(n, (ast.If, ast.While, ast.Return, ast.Assign))]
            if stmts:
                line = stmts[-1].lineno
                expanded.update(self.deps.select_lines({path: {line}}))
        scope = [(i, n) for i, n in enumerate(a) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if not scope:
            return expanded
        i, function = scope[-1]
        if i >= len(delta) or delta[i] != "body" or not self.file_entries(path):
            return expanded
        # Altering a dead function body can still affect an inspecting test;
        # callers add source readers separately. Edits to decorators and defaults
        # must be treated as changes to their enclosing scope.
        start = min([function.lineno] + [d.lineno for d in function.decorator_list])
        hits = 0
        for key in self.file_entries(path):
            if key.startswith(f"fn:{path}:{start}:") and key.rsplit(":", 1)[-1].split(".")[-1] == function.name:
                hits |= self.fact(key, "ran")
        if hits & self.startup:
            return expanded
        entrants = self.deps.numbers(hits)
        edited = b[i]
        if isinstance(edited, type(function)) and self.bindings(function) != self.bindings(edited):
            return entrants | expanded
        return expanded & entrants

    @staticmethod
    def bindings(function):
        names = set()
        for node in ast.walk(function):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                names.add(node.id)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                names.update((type(node).__name__, name) for name in node.names)
        return names

    @lru_cache(maxsize=128)
    def file_entries(self, path):
        return [k for k in self.sites if k.startswith(f"fn:{path}:")]

    def select(self, diff, old_changes):
        parsed = read_patch(diff)
        if set(parsed) != set(old_changes):
            raise ValueError("unrecognized file in patch")
        chosen = set()
        for path, hunks in parsed.items():
            base = self.deps.select_lines({path: old_changes[path]})
            if not path.endswith(".py"):
                chosen.update(base)
                continue
            source, before = self.source(path)
            updated = apply_hunks(source, hunks)
            compile(updated, path, "exec", dont_inherit=True)
            after = ast.parse(updated, type_comments=True)
            mask = self.fact("read:" + path, "read") & ~self.startup
            # Treat encoding declarations and interpreter directives conservatively.
            if source.splitlines()[:2] != updated.splitlines()[:2] and any(
                re.search(r"coding[:=]|^#!", x) for x in (source.splitlines()[:2] + updated.splitlines()[:2])
            ):
                chosen.update(base)
                continue
            for delta in differences(before, after):
                part = self.masks_for_change(path, before, after, delta)
                if part is None or part & self.startup:
                    chosen.update(self.ordinary(path, before, after, delta, base))
                else:
                    mask |= part
            chosen.update(self.deps.numbers(mask))
        return chosen | self.deps.unverified | self.unverified
