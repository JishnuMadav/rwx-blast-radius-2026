"""Record executed guard bodies and observable uses of explicitly passed arguments."""
import ast
import builtins
from collections import defaultdict
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
import sys
import types

import profile_semantics as base


active = ContextVar("argument_readers", default={})
models = {}
optional = set()
PURE_TRUTH = (bool, type(None), int, float, str, bytes, tuple, list, set, frozenset, dict)


def effect(keys):
    current = active.get()
    for key in keys:
        if key.startswith("if:"):
            base.profile.hit(base.profile.site(key), "effect")
        else:
            for site in current.get(key, ()):
                base.profile.hit(site, "effect")


@lru_cache(None)
def plain_attribute(cls, name):
    if type(cls).__getattribute__ is not type.__getattribute__ or cls.__getattribute__ is not object.__getattribute__:
        return False
    for parent in cls.__mro__:
        if name in vars(parent):
            value = vars(parent)[name]
            return type(value) is types.MemberDescriptorType or not hasattr(type(value), "__get__")
    return not any("__getattr__" in vars(parent) for parent in cls.__mro__)


def read(obj, name, keys):
    if not plain_attribute(type(obj), name):
        effect(keys)
    try:
        return getattr(obj, name)
    except BaseException:
        effect(keys)
        raise


def truth(value, keys):
    cls = type(value)
    if cls not in PURE_TRUTH and any("__bool__" in vars(c) or "__len__" in vars(c) for c in cls.__mro__):
        effect(keys)
    return value


def register(pairs):
    def decorate(fn):
        for name, key in pairs:
            models[fn.__code__, name] = key
        return fn
    return decorate


def invoke(fn, observers, forwarded, /, *args, **kwargs):
    old = active.get()
    new = None
    method = fn.__func__ if isinstance(fn, types.MethodType) else None
    code = method.__code__ if isinstance(method, types.FunctionType) else None
    def attach(param, sites):
        nonlocal new
        key = models.get((code, param))
        if key is None:
            for site in sites:
                base.profile.hit(site, "effect")
        elif sites:
            if new is None:
                new = dict(old)
            new[key] = tuple(set(new.get(key, ())) | set(sites))
    for param, site in observers:
        base.profile.hit(site, "ran")
        if code is None or param not in code.co_varnames[:code.co_argcount+code.co_kwonlyargcount]:
            base.profile.hit(site, "effect")
            continue
        params = code.co_varnames[:code.co_argcount]
        defaults = dict(zip(reversed(params), reversed(method.__defaults__ or ())))
        defaults.update(method.__kwdefaults__ or {})
        if param not in defaults or defaults[param] is not None:
            base.profile.hit(site, "effect")
        elif kwargs[param] is not None:
            attach(param, (site,))
    for caller, position in forwarded:
        sites = old.get(caller, ())
        if sites:
            if isinstance(position, int):
                param = code.co_varnames[position + 1] if code and position + 1 < code.co_argcount else None
            else:
                param = position
            attach(param, sites)
    if new is None:
        return fn(*args, **kwargs)
    token = active.set(new)
    try:
        return fn(*args, **kwargs)
    finally:
        active.reset(token)


def simple_guard(n):
    if isinstance(n, (ast.Name, ast.Constant)):
        return True
    if isinstance(n, ast.Attribute):
        return simple_guard(n.value)
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
        return simple_guard(n.operand)
    if isinstance(n, ast.BoolOp):
        return all(simple_guard(v) for v in n.values)
    return (isinstance(n, ast.Compare) and len(n.ops) == 1 and isinstance(n.ops[0], (ast.Is, ast.IsNot))
            and simple_guard(n.left) and simple_guard(n.comparators[0]))


def observable_guard(n):
    if isinstance(n, ast.Call):
        return (isinstance(n.func, ast.Attribute) and n.func.attr == "get"
                and bool(n.args) and len(n.args) <= 2 and not n.keywords
                and all(isinstance(v, ast.Constant) for v in n.args) and type(n.args[0].value) is str
                and observable_guard(n.func.value))
    if isinstance(n, ast.BoolOp):
        return all(observable_guard(v) for v in n.values)
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
        return observable_guard(n.operand)
    if isinstance(n, ast.Compare) and len(n.ops) == 1 and isinstance(n.ops[0], (ast.Is, ast.IsNot)):
        return observable_guard(n.left) and observable_guard(n.comparators[0])
    return simple_guard(n)


def getkey(obj, keys, *args):
    if type(obj) is not dict or not all(type(k) is str for k in obj):
        effect(keys)
    return obj.get(*args)


def protected(test, param):
    if not simple_guard(test):
        return False
    first = test.values[0] if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And) else test
    return (isinstance(first, ast.Name) and first.id == param or
            isinstance(first, ast.Compare) and isinstance(first.ops[0], ast.IsNot)
            and isinstance(first.left, ast.Name) and first.left.id == param
            and isinstance(first.comparators[0], ast.Constant) and first.comparators[0].value is None)


def transparent(fn, param, parents):
    uses = [n for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == param]
    guards, forwards = set(), {}
    for n in uses:
        if not isinstance(n.ctx, ast.Load):
            return None
        at = n
        while at is not fn:
            parent = parents[at]
            if isinstance(parent, ast.If) and not parent.orelse and protected(parent.test, param):
                guards.add(parent)
                break
            at = parent
        else:
            parent = parents[n]
            call = parents.get(parent) if isinstance(parent, ast.keyword) else parent
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name) and call.func.value.id == fn.args.args[0].arg
                    and not any(isinstance(a, ast.Starred) for a in call.args)
                    and all(k.arg is not None for k in call.keywords)):
                return None
            if n in call.args:
                dest = call.args.index(n)
            elif isinstance(parent, ast.keyword):
                dest = parent.arg
            else:
                return None
            forwards.setdefault(call, []).append(dest)
    return (guards, forwards) if uses else None


def parameter_plans(fn):
    if (fn.decorator_list or not fn.args.args or fn.args.posonlyargs or fn.args.vararg or fn.args.kwarg
        or any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.Yield, ast.YieldFrom))
               and n is not fn for n in ast.walk(fn))
        or any(isinstance(n, ast.Name) and n.id in ("locals", "vars", "eval", "exec", "inspect", "sys")
               for n in ast.walk(fn))):
        return {}
    parents = {c:n for n in ast.walk(fn) for c in ast.iter_child_nodes(n)}
    return {p.arg: plan for p in fn.args.args[1:] + fn.args.kwonlyargs
            if (plan := transparent(fn, p.arg, parents)) is not None}


def safe_argument(n):
    if isinstance(n, (ast.Name, ast.Constant)):
        return True
    # The input expression may still have side effects. The observer checks the
    # concrete isinstance implementation and type separately for conditional inputs.
    return (isinstance(n, ast.IfExp) and isinstance(n.body, ast.Name) and isinstance(n.orelse, ast.Constant)
            and n.orelse.value is None and isinstance(n.test, ast.Call)
            and isinstance(n.test.func, ast.Name) and n.test.func.id == "isinstance"
            and len(n.test.args) == 2 and not n.test.keywords and isinstance(n.test.args[0], ast.Name)
            and n.test.args[0].id == n.body.id and isinstance(n.test.args[1], ast.Attribute)
            and isinstance(n.test.args[1].value, ast.Name))


def isa(func, value, module, name, site):
    if type(module) is not types.ModuleType or name not in vars(module):
        base.profile.hit(site, "effect")
    cls = getattr(module, name)
    if func is not isinstance or not isinstance(cls, type) or type(cls).__instancecheck__ is not type.__instancecheck__:
        base.profile.hit(site, "effect")
    return func(value, cls)


class Transform(ast.NodeTransformer):
    wrap = staticmethod(base.Transform.wrap)
    def __init__(self, profile, path):
        self.p, self.path = profile, path
        self.guards, self.forward = defaultdict(list), defaultdict(list)

    def visit_ClassDef(self, node):
        for fn in node.body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            pairs = []
            for param, (guards, calls) in parameter_plans(fn).items():
                key = f"{self.path}:{fn.lineno}:{param}"
                pairs.append((param,key))
                for guard in guards:
                    self.guards[guard].append(key)
                for call, destinations in calls.items():
                    self.forward[call].extend((key, dest) for dest in destinations)
            if pairs:
                fn.decorator_list.append(self.wrap(fn, "_rwx_effect_register", [ast.Constant(tuple(pairs))]))
        return self.generic_visit(node)

    def instrument(self, test, keys, boolean=True):
        if isinstance(test, ast.BoolOp):
            test.values = [self.instrument(v, keys) for v in test.values]
            return test
        if isinstance(test, ast.UnaryOp):
            test.operand = self.instrument(test.operand, keys)
            return test
        if isinstance(test, ast.Compare):
            test.left = self.instrument(test.left, keys, False)
            test.comparators = [self.instrument(v, keys, False) for v in test.comparators]
            return test
        if isinstance(test, ast.Attribute):
            test = self.wrap(test, "_rwx_effect_read", [self.instrument(test.value, keys, False),
                                                        ast.Constant(test.attr), keys])
        elif isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute) and test.func.attr == "get":
            test = self.wrap(test, "_rwx_effect_getkey", [self.instrument(test.func.value, keys, False), keys, *test.args])
        return self.wrap(test, "_rwx_effect_truth", [test, keys]) if boolean else test

    def visit_If(self, node):
        keys = list(self.guards[node])
        if not node.orelse and observable_guard(node.test):
            label = f"if:{self.path}:{node.lineno}:{node.col_offset}"
            self.p.hit(self.p.site(label), "registered")
            keys.append(label)
        self.generic_visit(node)
        if keys:
            labels = ast.Constant(tuple(keys))
            node.test = self.instrument(node.test, labels)
            node.body.insert(0, ast.copy_location(ast.Expr(self.wrap(node, "_rwx_effect_hit", [labels])), node))
            # A deleted guard may have raised before reaching its body.
            record = ast.Expr(self.wrap(node, "_rwx_effect_hit", [labels]))
            handler = ast.ExceptHandler(None, None, [record, ast.Raise(None, None)])
            return ast.copy_location(ast.Try([node], [handler], [], []), node)
        return node

    def visit_Call(self, node):
        obs = []
        if isinstance(node.func, ast.Attribute):
            for kw in node.keywords:
                if (node.func.attr, kw.arg) in optional and safe_argument(kw.value):
                    site = self.p.site(f"kw:{self.path}:{node.lineno}:{node.col_offset}:{kw.arg}")
                    obs.append((kw.arg,site))
                    if isinstance(kw.value, ast.IfExp):
                        call = kw.value.test
                        cls = call.args[1]
                        kw.value.test = self.wrap(call, "_rwx_effect_isa", [call.func, call.args[0], cls.value,
                                                                          ast.Constant(cls.attr), ast.Constant(site)])
        forwarded = self.forward[node]
        self.generic_visit(node)
        if obs or forwarded:
            node.args = [node.func, ast.Constant(tuple(obs)), ast.Constant(tuple(forwarded)), *node.args]
            node.func = ast.copy_location(ast.Name("_rwx_effect_invoke", ast.Load()), node)
        return node


if __name__ == "__main__":
    for path in (Path(sys.argv[1])/"sqlglot").rglob("*.py"):
        for cls in ast.walk(ast.parse(path.read_bytes())):
            if not isinstance(cls, ast.ClassDef):
                continue
            for fn in cls.body:
                if isinstance(fn, ast.FunctionDef):
                    args = fn.args.args
                    candidates = {arg.arg for arg, d in zip(reversed(args), reversed(fn.args.defaults))
                                  if isinstance(d, ast.Constant) and d.value is None}
                    if not candidates:
                        continue
                    plans = parameter_plans(fn)
                    for arg, d in zip(reversed(args), reversed(fn.args.defaults)):
                        if arg.arg in candidates and arg.arg in plans:
                            optional.add((fn.name,arg.arg))
    builtins._rwx_effect_isa = isa
    builtins._rwx_effect_getkey = getkey
    builtins._rwx_effect_register = register
    builtins._rwx_effect_read = read
    builtins._rwx_effect_truth = truth
    builtins._rwx_effect_hit = effect
    builtins._rwx_effect_invoke = invoke
    base.Transform = Transform
    base.Profile.entry = lambda self, code, offset: sys.monitoring.DISABLE
    base.Profile.audit = lambda self, event, args: None
    base.main()
