"""Trace value dependencies of validation loops over keys derived from Boolean maps."""
import ast
import builtins
import sys

import profile_branches as safe
import profile_semantics as base


tables = {}


def validate(obj, name, values):
    p = base.profile
    cls = type(obj)
    source = next((tables.get((c,name)) for c in cls.__mro__ if name in vars(c)), None)
    if source is None:
        return getattr(obj,name)
    site, keys = source
    req = getattr(obj,name)
    args = safe.stored(obj,values)
    if type(req) is not set or type(args) is not dict or not all(type(k) is str for k in args):
        p.hit(site,"all")
        return req
    p.hit(site,"validated")
    for k in keys:
        v = args.get(k)
        if (isinstance(v,list) and type(v) is not list
            or type(v).__getattribute__ is not object.__getattribute__ and type(v) not in safe.SCALARS+safe.CONTAINERS):
            p.hit(site,"all")
            break
        if v is None or type(v) is list and not v:
            p.hit(site,"invalid:" + k)
            if k in req:
                # A size change can reorder diagnostics for other invalid keys.
                p.hit(site,"all")
                break
    return req


def shape(loop):
    if (not isinstance(loop.target,ast.Name) or not isinstance(loop.iter,ast.Attribute)
        or not isinstance(loop.iter.value,ast.Name) or len(loop.body)!=2 or loop.orelse):
        return None
    a, guard = loop.body
    if not (isinstance(a,ast.Assign) and len(a.targets)==1 and isinstance(a.targets[0],ast.Name)
            and isinstance(a.value,ast.Call) and isinstance(a.value.func,ast.Attribute) and a.value.func.attr=="get"
            and not a.value.keywords and len(a.value.args)==1 and isinstance(a.value.args[0],ast.Name)
            and a.value.args[0].id==loop.target.id and isinstance(a.value.func.value,ast.Attribute)
            and isinstance(a.value.func.value.value,ast.Name) and a.value.func.value.value.id==loop.iter.value.id
            and isinstance(guard,ast.If) and not guard.orelse):
        return None
    v = a.targets[0].id
    expected = ast.parse(f"{v} is None or (isinstance({v}, list) and not {v})",mode="eval").body
    return (a.value.func.value.attr, {v,loop.target.id}) if ast.dump(guard.test)==ast.dump(expected) else None


class Transform(base.Transform):
    def visit_FunctionDef(self, fn):
        for i,loop in enumerate(fn.body):
            if not isinstance(loop,ast.For) or not (plan := shape(loop)):
                continue
            field, names = plan
            if any(isinstance(n,ast.Name) and n.id in names for s in fn.body[i+1:] for n in ast.walk(s)):
                continue
            if any(isinstance(n,ast.Name) and n.id in {"locals","vars","exec","eval"} for n in ast.walk(fn)):
                continue
            target=loop.iter
            loop.iter=self.wrap(target,"_rwx_validator",[target.value,ast.Constant(target.attr),ast.Constant(field)])
        return self.generic_visit(fn)

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx,ast.Load) and node.attr in self.p.derived_names:
            return self.wrap(node,"_rwx_attribute",[node.value,ast.Constant(node.attr)])
        return node

    def visit_Compare(self,node):
        return self.generic_visit(node)

    def visit_ClassDef(self,node):
        self.generic_visit(node)
        for stmt in node.body:
            if isinstance(stmt,(ast.Assign,ast.AnnAssign)) and isinstance(stmt.value,ast.Dict):
                d=stmt.value
                if d.keys and all(isinstance(k,ast.Constant) and type(k.value) is str for k in d.keys) and all(
                        isinstance(v,ast.Constant) and type(v.value) is bool for v in d.values):
                    site=self.p.site(f"validator:{self.path}:{d.lineno}:{d.col_offset}")
                    stmt.value=self.wrap(d,"_rwx_mapping",[ast.Constant(site),d])
        return node


if __name__ == "__main__":
    builtins._rwx_validator = validate
    base.Transform = Transform
    original = base.Profile.keyset
    def derived(self,owner,name,mapping):
        out=original(self,owner,name,mapping)
        if isinstance(mapping,base.ObservedDict) and isinstance(owner,type):
            tables[owner,name]=(mapping.site,tuple(dict.keys(mapping)))
        return out
    base.Profile.keyset = derived
    base.Profile.entry = lambda self, code, offset: sys.monitoring.DISABLE
    base.Profile.audit = lambda self, event, args: None
    base.main()
