"""Interpret a patch using source and execution evidence from the pinned revision."""
import ast
from os.path import commonprefix
import dis
from functools import lru_cache
import gzip
import json
import operator
from pathlib import Path, PurePosixPath
import re
import types
from structure import aligned, changed_dispatch_keys


HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
RELATIONS = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
             ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


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


def semantic_lines(nodes, changed):
    for node in reversed(nodes):
        if isinstance(node, ast.AST) and hasattr(node, "lineno"):
            anchored = {line for line in changed if node.lineno <= line <= node.end_lineno}
            return anchored or changed
    return changed


def inserted_raise(before, after):
    if len(after.body) != len(before.body) + 1:
        return False
    for i, statement in enumerate(after.body):
        if isinstance(statement, ast.Raise):
            remainder = after.body[:i] + after.body[i + 1:]
            if all(tree_key(a) == tree_key(b) for a, b in zip(before.body, remainder)):
                return True
    return False


class Evidence:
    def __init__(self, root, data, deps, lifecycle=None, imports=None):
        self.root = root
        self.deps = deps
        self.sites = data["facts"]
        if data["ids"] != deps.ids:
            raise ValueError("baseline profile catalogs differ")
        self.masks = [int(value, 16) for value in data["masks"]]
        self.startup = 1 << len(deps.ids)
        self.unverified = {deps.number[n] for n in data.get("unverified", ()) if n in deps.number}
        lifecycle = lifecycle or {"ids": deps.ids, "files": {}, "masks": []}
        if lifecycle["ids"] != deps.ids:
            raise ValueError("lifecycle profile catalog differs")
        self.lifecycle = lifecycle["files"]
        self.lifecycle_masks = [int(value, 16) for value in lifecycle["masks"]]
        if imports is not None and imports["ids"] != deps.ids:
            raise ValueError("isolated import catalog differs")
        self.imports = imports or {}

    def lifecycle_hits(self, path, lines):
        covered = self.lifecycle.get(path, {})
        mask = 0
        for line in lines:
            index = covered.get(str(line))
            if index is not None:
                mask |= self.lifecycle_masks[index]
        return self.deps.numbers(mask)

    def fact(self, site, key):
        facts = self.sites.get(site, {})
        return self.masks[facts[key]] if key in facts else 0

    @lru_cache(maxsize=1024)
    def setting_reads(self, site, key):
        mask = self.fact(site, key)
        if not mask & self.startup:
            return mask
        prefix = key + "|"
        origins = [k[len(prefix):] for k in self.sites.get("origin:" + site, {}) if k.startswith(prefix)]
        cold = self.fact("cold:" + site, key)
        if not cold or not origins or not self.imports or self.imports.get("unverified"):
            return mask
        for path in origins:
            module = path[:-3].replace("/", ".").removesuffix(".__init__")
            if not path.startswith("sqlglot/") or module not in self.imports["modules"]:
                return mask
            cold |= self.module_mask(path)
        return (mask & ~self.startup) | cold

    @lru_cache(maxsize=128)
    def module_mask(self, path):
        readers = self.import_dependents(path)
        return sum(1 << i for i, test in enumerate(self.deps.ids) if self.deps.number.get(test) in readers)

    @lru_cache(maxsize=128)
    def import_dependents(self, path):
        name = path[:-3].replace("/", ".").removesuffix(".__init__")
        modules = self.imports.get("modules", {})
        if name not in modules:
            return self.deps.all_tests
        mask = int(modules[name], 16) | (self.fact("read:" + path, "read") & ~self.startup)
        for index in set(self.deps.files.get(path, {}).values()):
            mask |= self.deps.masks[index]
        for index in set(self.lifecycle.get(path, {}).values()):
            mask |= self.lifecycle_masks[index]
        skipped = {self.deps.number[test] for test in self.imports["unverified"] if test in self.deps.number}
        return self.deps.numbers(mask) | skipped

    @lru_cache(maxsize=96)
    def source(self, path):
        text = (self.root / path).read_text(encoding="utf-8-sig")
        return text, ast.parse(text)

    def masks_for_change(self, path, before, after, delta, isolated=False, new_delta=None):
        a = ancestors(before, delta)
        b = ancestors(after, delta if new_delta is None else new_delta)
        for x, y in zip(a, b):
            if (isinstance(x, ast.BoolOp) and isinstance(y, ast.BoolOp) and type(x.op) is not type(y.op)
                and len(x.values) == len(y.values) == 2
                and all(tree_key(s) == tree_key(t) for s, t in zip(x.values, y.values))):
                prop = f"property-guard:{path}:{x.lineno}:{x.col_offset}"
                if isolated and self.fact(prop,"ran") and not self.fact(prop,"ran") & self.startup:
                    return self.fact(prop,"possible")
                site = f"branch:{path}:{x.lineno}:{x.col_offset}"
                if self.fact(site, "ran"):
                    return self.fact(site, "different")
        for old, new in reversed(list(zip(a, b))):
            if not isinstance(old, ast.Call) or not isinstance(new, ast.Call):
                continue
            if a[-1] is not old.keywords or len(old.keywords) != len(new.keywords) + 1:
                continue
            if (tree_key(old.func) != tree_key(new.func) or len(old.args) != len(new.args)
                or any(tree_key(x) != tree_key(y) for x, y in zip(old.args, new.args))):
                continue
            for i, kw in enumerate(old.keywords):
                if not kw.arg or not all(tree_key(x) == tree_key(y) for x, y in zip(
                    old.keywords[:i] + old.keywords[i+1:], new.keywords)):
                    continue
                site = f"kw:{path}:{old.lineno}:{old.col_offset}:{kw.arg}"
                if self.fact(site, "ran"):
                    return self.fact(site, "effect")
        for old, new in reversed(list(zip(a, b))):
            if not isinstance(old, ast.Compare) or not isinstance(new, ast.Compare):
                continue
            numeric = f"number:{path}:{old.lineno}:{old.col_offset}"
            if (numeric in self.sites and len(old.ops) == len(new.ops) == 1
                and type(old.ops[0]) in RELATIONS and type(new.ops[0]) in RELATIONS
                and tree_key(old.left) == tree_key(new.left)
                and all(isinstance(x.comparators[0], ast.Constant) and type(x.comparators[0].value) is int
                        for x in (old, new))):
                left_op, right_op = RELATIONS[type(old.ops[0])], RELATIONS[type(new.ops[0])]
                result = self.fact(numeric, "unknown")
                for value in self.sites[numeric]:
                    if value != "unknown" and (left_op(int(value), old.comparators[0].value)
                                               != right_op(int(value), new.comparators[0].value)):
                        empty = f"empty:{path}:{old.lineno}:{old.col_offset}"
                        if value == "0" and isolated and self.fact(empty, "ran"):
                            result |= self.fact(empty, "unknown")
                        else:
                            result |= self.fact(numeric, value)
                return result
            if (len(old.ops) != 1 or len(new.ops) != 1 or tree_key(old.left) != tree_key(new.left)
                or tree_key(old.comparators[0]) != tree_key(new.comparators[0])):
                continue
            pair = {type(old.ops[0]).__name__, type(new.ops[0]).__name__}
            if pair in ({"Is","Eq"},{"IsNot","NotEq"}):
                identity = f"none:{path}:{old.lineno}:{old.col_offset}"
                if self.fact(identity, "ran"):
                    return self.fact(identity, "possible")
            if isolated and pair == {"Eq","NotEq"}:
                masked = f"masked:{path}:{old.lineno}:{old.col_offset}"
                if self.fact(masked, "ran"):
                    return self.fact(masked, "possible")
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
                    dispatch = f"{path}:{old_val.lineno}:{old_val.col_offset}:dispatch"
                    if a[-1] is old_val and self.fact(dispatch, "registered"):
                        keys = changed_dispatch_keys(old_val, new_val)
                        if keys is not None:
                            mask = self.fact(dispatch, "all")
                            for key in keys:
                                mask |= self.fact(dispatch, "key:" + key)
                            return mask
                    if self.fact(dispatch, "registered") and len(old_val.keys) == len(new_val.keys):
                        for key, replacement in zip(old_val.keys, new_val.keys):
                            if (isinstance(key, ast.Constant) and any(key is n for n in a)
                                and isinstance(replacement, ast.Constant)
                                and type(key.value) is str and type(replacement.value) is str):
                                return (self.fact(dispatch, "key:" + key.value)
                                        | self.fact(dispatch, "key:" + replacement.value)
                                        | self.fact(dispatch, "all"))
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
                                validator = f"validator:{path}:{old_val.lineno}:{old_val.col_offset}"
                                if (isolated and self.fact(validator, "validated") and type(key.value) is str
                                    and isinstance(expr, ast.Constant) and type(expr.value) is bool
                                    and isinstance(new_val.values[index], ast.Constant)
                                    and type(new_val.values[index].value) is bool):
                                    return (self.fact(validator, "invalid:" + key.value)
                                            | self.fact(validator, "v:" + repr(key.value)) | self.fact(validator, "all"))
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
                    scalar = f"scalar:{base}:{target.id}"
                    guard = f"guard:{base}:{target.id}"
                    forwarded = f"forward:{base}:{target.id}"
                    channels = [key for key in self.sites.get(forwarded,{}) if key.startswith("slot-use:")]
                    if (isolated and channels and isinstance(old_val,ast.Constant) and isinstance(new_val,ast.Constant)
                        and type(old_val.value) is bool and type(new_val.value) is bool):
                        mask = (self.fact(forwarded,"direct") | self.setting_reads(f"classdict:{base}","all")
                                | self.setting_reads(f"classdict:{base}","key:" + target.id))
                        for channel in channels:
                            mask |= self.fact(channel,"used")
                        if not mask & self.startup:
                            return mask
                    switch = f"switch:{base}:{target.id}"
                    if (isolated and self.fact(switch,"analyzed") and not self.fact(switch,"read") & self.startup
                        and not self.fact(site,"read") & self.startup and isinstance(old_val,ast.Constant)
                        and isinstance(new_val,ast.Constant) and type(old_val.value) is bool and type(new_val.value) is bool):
                        return (self.fact(switch,"read") | self.setting_reads(f"classdict:{base}","all")
                                | self.setting_reads(f"classdict:{base}","key:" + target.id))
                    copied = f"copy:{base}:{target.id}"
                    copy_keys = [k for k in self.sites.get(copied, {}) if k.startswith("writes:")]
                    if (isolated and copy_keys and isinstance(old_val, ast.Constant) and isinstance(new_val, ast.Constant)
                        and type(old_val.value) is bool and type(new_val.value) is bool
                        and not self.fact(site, "read") & self.startup):
                        readers = self.fact(copied, "read") | self.fact(f"classdict:{base}", "all")
                        readers |= self.fact(f"classdict:{base}", "key:" + target.id)
                        for key in copy_keys:
                            writer = self.fact(copied, key)
                            consumer = self.fact("copy-consumer:" + key.removeprefix("writes:"), "possible")
                            readers |= (writer & consumer) | ((writer | consumer) & self.startup)
                        return readers
                    if (isolated and self.fact(guard, "analyzed") and not self.fact(site, "read") & self.startup
                        and isinstance(old_val, ast.Constant) and isinstance(new_val, ast.Constant)
                        and type(old_val.value) is bool and type(new_val.value) is bool):
                        writer = self.fact(guard, "append")
                        consumer = self.fact("consumer:" + target.id, "possible")
                        return (self.fact(guard, "read") | self.fact(f"classdict:{base}", "all")
                                | self.fact(f"classdict:{base}", "key:" + target.id)
                                | (writer & consumer) | ((writer | consumer) & self.startup))
                    meta = f"metascalar:{base}:{target.id}"
                    if (self.fact(meta, "installed") and isinstance(old_val, ast.Constant)
                        and isinstance(new_val, ast.Constant) and type(old_val.value) is type(new_val.value)):
                        return (self.setting_reads(meta, "read") | self.setting_reads(site, "read")
                                | self.setting_reads(f"classdict:{base}", "key:" + target.id)
                                | self.setting_reads(f"classdict:{base}", "all"))
                    if (self.fact(scalar, "installed") and isinstance(old_val, ast.Constant)
                        and isinstance(new_val, ast.Constant) and type(old_val.value) is type(new_val.value)):
                        return (self.setting_reads(scalar, "read") | self.setting_reads(f"namespace:{base}", "read")
                                | self.setting_reads(site, "read"))
                    if site in self.sites:
                        return self.fact(site, "read")
        return None

    def ordinary(self, path, before, after, delta, base, new_delta=None):
        a = ancestors(before, delta)
        b = ancestors(after, delta if new_delta is None else new_delta)
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
        if isinstance(b[i], type(function)) and self.generator(function) != self.generator(b[i]):
            # Calling a generator without iterating it leaves no function-entry
            # event. Changing between ordinary and lazy calls affects those callers.
            return self.import_dependents(path) if path.startswith("sqlglot/") else self.deps.all_tests
        # A single-line computed expression with its own bytecode line event
        # cannot influence a test which never entered that statement. Missing
        # coverage on such an executable line represents zero observed hits.
        statements = [n for n in a[i + 1:] if isinstance(n, (ast.Return, ast.Assign, ast.Expr, ast.Raise))]
        if statements and isinstance(b[i], type(function)):
            stmt = statements[-1]
            if (stmt.lineno == stmt.end_lineno and stmt.lineno != function.lineno
                and stmt.lineno in self.executable_lines(path) and self.bindings(function) == self.bindings(b[i])
                and not any(isinstance(n, (ast.Yield, ast.YieldFrom, ast.Await, ast.Lambda))
                            for n in (*a, *b)) and isinstance(a[-1], (int, str, float, bool, ast.operator,
                            ast.unaryop, ast.cmpop)) and isinstance(b[-1], type(a[-1]))):
                index = self.deps.files.get(path, {}).get(str(stmt.lineno))
                if index is None:
                    return set()
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
            # A changed local can affect the whole function, including a nested
            # closure. A caller must still enter this function to see the change.
            # Crossing the yield boundary also changes the call protocol itself.
            return entrants | self.lifecycle_hits(path, range(function.body[0].lineno, function.end_lineno + 1))
        return expanded & entrants

    @lru_cache(maxsize=64)
    def executable_lines(self, path):
        code = compile(self.source(path)[0], path, "exec", dont_inherit=True)
        lines = set()
        def visit(block):
            lines.update(n for _, n in dis.findlinestarts(block) if n is not None)
            for const in block.co_consts:
                if isinstance(const, types.CodeType):
                    visit(const)
        visit(code)
        return lines

    @staticmethod
    def bindings(function):
        names = set()
        for node in ast.walk(function):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                names.add(node.id)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                names.update((type(node).__name__, name) for name in node.names)
        return names

    @staticmethod
    @lru_cache(maxsize=1024)
    def generator(function):
        todo = list(function.body)
        while todo:
            node = todo.pop()
            if isinstance(node, (ast.Yield, ast.YieldFrom)):
                return True
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                todo.extend(ast.iter_child_nodes(node))
        return False

    @staticmethod
    @lru_cache(maxsize=1024)
    def loads_modules(fn):
        return any(isinstance(n, ast.Call) and
                   (isinstance(n.func, ast.Name) and n.func.id == "__import__"
                    or isinstance(n.func, ast.Attribute) and n.func.attr in {"import_module","entry_points"})
                   for n in ast.walk(fn))

    @staticmethod
    def predicate_leaves(node):
        if isinstance(node,ast.BoolOp):
            return tuple(value for part in node.values for value in Evidence.predicate_leaves(part))
        return (tree_key(node),)

    def predicate_reach(self,path,nodes,updated):
        if not self.fact("cold:predicate-catalog","complete"):
            return None
        for a,b in reversed(list(zip(nodes,updated))):
            if isinstance(a,ast.BoolOp) and isinstance(b,ast.BoolOp):
                matches = self.predicate_leaves(a)==self.predicate_leaves(b)
            elif isinstance(a,ast.Compare) and isinstance(b,ast.Compare):
                matches = (tree_key(a.left)==tree_key(b.left) and len(a.ops)==len(b.ops)==1
                           and tree_key(a.comparators[0])==tree_key(b.comparators[0]))
            else:
                continue
            if matches:
                site=f"predicate:{path}:{a.lineno}:{a.col_offset}:{type(a).__name__}:{a.end_lineno}:{a.end_col_offset}"
                if self.fact(site,"registered"):
                    mask=self.fact(site,"ran")
                    if mask & self.startup or self.fact("cold:"+site,"ran"):
                        return None
                    if any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and self.loads_modules(n) for n in nodes):
                        return None
                    return mask
        return None

    @staticmethod
    def private_to_gap(fn, replacements, names):
        omitted = {id(n) for stmt in replacements for n in ast.walk(stmt)}
        if not all(isinstance(name, str) for name in names):
            return False
        if any(isinstance(n, ast.Name) and n.id in {"locals", "vars", "eval", "exec", "globals", "super"}
               or isinstance(n, ast.Attribute) and n.attr in {"f_locals", "_getframe", "currentframe"}
               for n in ast.walk(fn)):
            return False
        return not any(id(n) not in omitted and (isinstance(n, ast.Name) and n.id in names
                       or isinstance(n, ast.arg) and n.arg in names) for n in ast.walk(fn))

    @lru_cache(maxsize=128)
    def file_entries(self, path):
        return [k for k in self.sites if k.startswith(f"fn:{path}:")]

    def statement_gap(self, path, previous, current, gap):
        i, j, k, l = gap
        old, new = previous[-1], current[-1]
        removed, added = old[i:j], new[k:l]
        funcs = [(ix, n) for ix, n in enumerate(previous) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if funcs:
            ix, fn = funcs[-1]
            updated = current[ix]
            if isinstance(updated, type(fn)) and self.generator(fn) != self.generator(updated):
                return self.import_dependents(path) if path.startswith("sqlglot/") else self.deps.all_tests
            start = min([fn.lineno] + [d.lineno for d in fn.decorator_list])
            owners = [n.name for n in previous[:ix+1] if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
            # Qualnames include <locals> for nested functions.
            entries = [s for s in self.file_entries(path) if s.startswith(f"fn:{path}:{start}:")
                       and s.rsplit(":",1)[-1].replace(".<locals>.", ".") == ".".join(owners)]
            if not entries:
                return self.import_dependents(path)
            mask = self.fact(entries[0], "ran")
            hits = self.deps.numbers(mask & ~self.startup)
            # File-isolated evidence distinguishes import-time calls from declarations.
            body_lines = range(fn.body[0].lineno, fn.end_lineno + 1)
            hits |= self.lifecycle_hits(path, body_lines)
            # A new local binding, generator or nested scope changes the entire function.
            different = self.bindings(fn) ^ self.bindings(updated) if isinstance(updated, type(fn)) else set()
            if (not isinstance(updated, type(fn)) or different and not
                (self.private_to_gap(fn, removed, different) and self.private_to_gap(updated, added, different))
                or any(isinstance(n, (ast.Yield, ast.YieldFrom, ast.Await, ast.FunctionDef, ast.ClassDef,
                                      ast.Global, ast.Nonlocal)) for s in removed + added for n in ast.walk(s))):
                return hits
            # A deletion can only matter where one of the deleted statements ran.
            # At a pure insertion the first old statement following it was reachable
            # on that path. At the end, reaching the previous statement is necessary.
            semantic = set()
            if removed and not added:
                anchors = []
                for stmt in removed:
                    site = f"if:{path}:{stmt.lineno}:{stmt.col_offset}"
                    if isinstance(stmt, ast.If) and not stmt.orelse and self.fact(site, "registered"):
                        effects = self.fact(site, "effect")
                        if not effects & self.startup:
                            semantic |= self.deps.numbers(effects)
                            semantic |= self.lifecycle_hits(path, {stmt.lineno})
                            continue
                    anchors.append(stmt)
                if not anchors:
                    return semantic
            else:
                anchors = removed or old[i:i+1] or old[i-1:i]
            if not anchors:
                return hits
            lines = {s.lineno for s in anchors}
            table = self.deps.files.get(path, {})
            if not all(str(n) in table for n in lines):
                return hits
            selected = set()
            for n in lines:
                selected |= self.deps.numbers(self.deps.masks[table[str(n)]])
            return semantic | (hits & selected) | self.lifecycle_hits(path, lines)
        classes = [n.name for n in previous if isinstance(n, ast.ClassDef)]
        if classes and removed and not added:
            owner = path[:-3].replace("/", ".") + "." + ".".join(classes)
            readers = self.fact(f"classdict:{owner}", "all") | self.fact(f"namespace:{owner}", "read")
            for stmt in removed:
                target = stmt.targets[0] if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 else getattr(stmt, "target", None)
                if (not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or not isinstance(target, ast.Name)
                    or not isinstance(stmt.value, ast.Constant) or type(stmt.value.value) not in (str, bool, int)
                    or isinstance(stmt, ast.AnnAssign)):
                    break
                key = target.id
                scalar, meta = f"scalar:{owner}:{key}", f"metascalar:{owner}:{key}"
                if not (self.fact(scalar, "installed") or self.fact(meta, "installed")):
                    break
                readers |= (self.fact(scalar, "read") | self.fact(meta, "read") | self.fact(f"attr:{owner}:{key}", "read")
                            | self.fact(f"classdict:{owner}", "key:" + key))
            else:
                if not readers & self.startup:
                    return self.deps.numbers(readers)
        if classes and removed and not added and all(isinstance(s, ast.FunctionDef) and not s.decorator_list for s in removed):
            owner = path[:-3].replace("/", ".") + "." + ".".join(classes)
            mask = self.fact("method-dict:" + owner, "read")
            for fn in removed:
                site = f"method:{owner}.{fn.name}"
                annotations = [n.annotation for n in ast.walk(fn.args) if isinstance(n, ast.arg)] + [fn.returns]
                postponed = any(isinstance(s, ast.ImportFrom) and s.module == "__future__"
                                and any(n.name == "annotations" for n in s.names) for s in previous[0].body)
                if (not self.fact(site, "registered") or fn.args.defaults or any(d is not None for d in fn.args.kw_defaults)
                    or any(annotations) and not postponed):
                    break
                mask |= self.fact(site, "read") | self.fact(f"fn:{path}:{fn.lineno}:{'.'.join(classes)}.{fn.name}", "ran")
            else:
                if not mask & self.startup:
                    return self.deps.numbers(mask)
        return self.import_dependents(path) if path.startswith("sqlglot/") else self.deps.all_tests

    def select(self, diff, old_changes):
        parsed = read_patch(diff)
        if set(parsed) != set(old_changes):
            raise ValueError("unrecognized file in patch")
        chosen = set()
        python_edits = []
        for path, hunks in parsed.items():
            base = self.deps.select_lines({path: old_changes[path]})
            if not path.endswith(".py"):
                chosen.update(base)
                chosen.update(self.lifecycle_hits(path, {0}))
                continue
            source, before = self.source(path)
            updated = apply_hunks(source, hunks)
            compile(updated, path, "exec", dont_inherit=True)
            after = ast.parse(updated)
            mask = self.fact("read:" + path, "read") & ~self.startup
            # Treat encoding declarations and interpreter directives conservatively.
            if source.splitlines()[:2] != updated.splitlines()[:2] and any(
                re.search(r"coding[:=]|^#!", x) for x in (source.splitlines()[:2] + updated.splitlines()[:2])
            ):
                chosen.update(base)
                chosen.update(self.lifecycle_hits(path, old_changes[path]))
                continue
            if path.startswith("sqlglot/") and self.imports and inserted_raise(before, after):
                chosen.update(self.import_dependents(path))
                continue
            deltas = list(aligned(before, after))
            python_edits.append((path, before, after, deltas, mask))
        # Every reader of an edited fixture has already been included. An
        # independent comment/fixture does not invalidate a single code proof.
        isolated = sum(len(item[3]) for item in python_edits) == 1
        changed_files = sum(bool(item[3]) for item in python_edits)
        for path, before, after, deltas, mask in python_edits:
            per_file = set()
            reach = None
            if changed_files == 1 and deltas and all(gap is None for _,_,gap in deltas):
                # All code edits must belong to this expression. Two separate
                # edits can enable a short-circuited operand for the first time.
                old_path = commonprefix([old for old,_,_ in deltas])
                new_path = commonprefix([new for _,new,_ in deltas])
                reach = self.predicate_reach(path,ancestors(before,old_path),ancestors(after,new_path))
            for delta, updated_delta, gap in deltas:
                nodes = ancestors(before, delta)
                if gap is not None:
                    updated_nodes = ancestors(after, updated_delta)
                    per_file.update(self.statement_gap(path, nodes, updated_nodes, gap))
                    continue
                lines = semantic_lines(nodes, old_changes[path])
                part = self.masks_for_change(path, before, after, delta, isolated, updated_delta)
                if reach is not None and (part is None or not part & self.startup):
                    mask |= reach if part is None else part & reach
                    continue
                inside_function = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                                      for n in nodes)
                if inside_function or part is None or part & self.startup:
                    per_file.update(self.lifecycle_hits(path, lines))
                if part is None or part & self.startup:
                    ordinary = self.ordinary(path, before, after, delta, self.deps.select_lines({path: lines}), updated_delta)
                    # A change during module construction can affect later lazy
                    # imports even when their individual tests never hit this line.
                    funcs = [n for n in nodes if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]
                    if (funcs and self.imports and path.startswith("sqlglot/") and
                        (self.loads_modules(funcs[-1]) or self.lifecycle_hits(path,lines))):
                        ordinary = self.import_dependents(path)
                    if ordinary == self.deps.all_tests and self.imports and path.startswith("sqlglot/"):
                        ordinary = self.import_dependents(path)
                    per_file.update(ordinary)
                else:
                    mask |= part
            chosen.update(per_file | self.deps.numbers(mask))
        return chosen | self.deps.unverified | self.unverified
