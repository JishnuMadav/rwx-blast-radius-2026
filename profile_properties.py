"""Observe type guards paired with proven side-effect-free property results."""
import ast
import builtins
from functools import lru_cache
import inspect
import sys
import textwrap
import types

import profile_branches as data
import profile_semantics as base


unknown = data.UNKNOWN


@lru_cache(None)
def definition(fn):
    if (type(fn) is not types.FunctionType or fn.__closure__ or hasattr(fn,"__wrapped__")
        or not fn.__module__.startswith("sqlglot.")):
        return None
    try:
        body = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
    except (OSError,ValueError,SyntaxError,TypeError):
        return None
    if (not isinstance(body,ast.FunctionDef) or len(body.args.args)!=1 or body.args.posonlyargs
        or body.args.kwonlyargs or body.args.vararg or body.args.kwarg
        or any(not isinstance(d,ast.Name) or d.id!="property" for d in body.decorator_list)):
        return None
    return body


def block(stmts,env,globals_,depth):
    for s in stmts:
        if isinstance(s,ast.Expr) and isinstance(s.value,ast.Constant) and type(s.value.value) is str:
            continue
        if isinstance(s,ast.Return):
            return True,evaluate(s.value,env,globals_,depth)
        if isinstance(s,ast.If):
            tests=data.safe_truth(evaluate(s.test,env,globals_,depth))
            if tests is unknown: return True,unknown
            returned,v=block(s.body if tests else s.orelse,env,globals_,depth)
            if returned: return True,v
        elif isinstance(s,ast.Try) and not s.finalbody and not s.orelse:
            # Only follow an exception-free return. Exceptions are unresolved.
            returned,v=block(s.body,env,globals_,depth)
            if returned: return True,v
        else:
            return True,unknown
    return False,None


def method(fn,obj,depth):
    tree=definition(fn)
    if tree is None or depth>12: return unknown
    return block(tree.body,{tree.args.args[0].arg:obj},fn.__globals__,depth+1)[1]


def attribute(obj,field,depth):
    if type(obj) is types.ModuleType:
        return vars(obj).get(field,unknown)
    desc=data.descriptor(type(obj),field)
    if type(desc) is property:
        return method(desc.fget,obj,depth)
    return data.stored(obj,field)


def evaluate(n,env,gl,depth):
    if depth>12: return unknown
    if isinstance(n,ast.Name):
        return env.get(n.id,gl.get(n.id,vars(builtins).get(n.id,unknown)))
    if isinstance(n,ast.Constant): return n.value
    if isinstance(n,ast.Attribute):
        obj=evaluate(n.value,env,gl,depth)
        return unknown if obj is unknown else attribute(obj,n.attr,depth)
    if isinstance(n,ast.BoolOp):
        for part in n.values[:-1]:
            val=evaluate(part,env,gl,depth)
            truth=data.safe_truth(val)
            if truth is unknown: return unknown
            if truth==isinstance(n.op,ast.Or): return val
        return evaluate(n.values[-1],env,gl,depth)
    if isinstance(n,ast.UnaryOp) and isinstance(n.op,ast.Not):
        v=data.safe_truth(evaluate(n.operand,env,gl,depth))
        return unknown if v is unknown else not v
    if isinstance(n,ast.Subscript):
        mapping=evaluate(n.value,env,gl,depth)
        key=evaluate(n.slice,env,gl,depth)
        if type(mapping) is dict and type(key) is str and all(type(k) is str for k in mapping):
            return mapping.get(key,unknown)
    if isinstance(n,ast.Call) and not n.keywords:
        args=[evaluate(a,env,gl,depth) for a in n.args]
        if any(a is unknown for a in args): return unknown
        if isinstance(n.func,ast.Name):
            fn=evaluate(n.func,env,gl,depth)
            if fn is isinstance and len(args)==2:
                obj,cls=args
                if (isinstance(cls,type) and type(cls).__instancecheck__ is type.__instancecheck__
                    and type(obj).__getattribute__ is object.__getattribute__
                    and data.descriptor(type(obj),"__class__") is vars(object)["__class__"]):
                    return any(parent is cls for parent in type(obj).__mro__)
                if type(obj) in data.SCALARS and cls in data.SCALARS: return isinstance(obj,cls)
            if fn is int and len(args)==1 and type(args[0]) in (int,bool,str):
                try: return int(args[0])
                except (ValueError,OverflowError): return unknown
        if isinstance(n.func,ast.Attribute):
            owner=evaluate(n.func.value,env,gl,depth)
            if owner is unknown: return unknown
            if n.func.attr=="get" and type(owner) is dict and len(args) in (1,2) and type(args[0]) is str and all(type(k) is str for k in owner):
                return owner.get(*args)
            if not args:
                raw=data.descriptor(type(owner),n.func.attr)
                if type(raw) is types.FunctionType:
                    dictionary=data.descriptor(type(owner),"__dict__")
                    if type(dictionary) is types.GetSetDescriptorType and n.func.attr in dictionary.__get__(owner):
                        return unknown
                    return method(raw,owner,depth)
    return unknown


@lru_cache(None)
def parse(expr):
    return ast.parse(expr,mode="eval").body


def observe(first,site,expr):
    p=base.profile
    p.hit(site,"ran")
    if (site,"possible") not in p.seen:
        frame=sys._getframe(1)
        # A missing comprehension local must never resolve to an unrelated global.
        expression=parse(expr)
        names={n.id for n in ast.walk(expression) if isinstance(n,ast.Name)}
        if not names.issubset(frame.f_locals) or type(first) is not bool:
            p.hit(site,"possible")
        else:
            val=evaluate(expression,frame.f_locals,frame.f_globals,0)
            if val is not first:
                p.hit(site,"possible")
    return first


class Transform(ast.NodeTransformer):
    wrap = staticmethod(base.Transform.wrap)

    def __init__(self,p,path):
        self.p,self.path=p,path

    def visit_BoolOp(self,n):
        if (len(n.values)==2 and isinstance(n.values[0],ast.Call) and isinstance(n.values[0].func,ast.Name)
            and n.values[0].func.id=="isinstance" and isinstance(n.values[1],ast.Attribute)):
            first,rhs=n.values
            site=self.p.site(f"property-guard:{self.path}:{n.lineno}:{n.col_offset}")
            n.values[0]=base.Transform.wrap(first,"_rwx_property_guard",[first,ast.Constant(site),ast.Constant(ast.unparse(rhs))])
        return self.generic_visit(n)


if __name__=="__main__":
    builtins._rwx_property_guard=observe
    base.Transform=Transform
    base.Profile.entry=lambda self,code,offset:sys.monitoring.DISABLE
    base.Profile.audit=lambda self,event,args:None
    base.main()
