"""Follow Boolean configuration values into plain constructor slots and local aliases."""
import ast
import builtins
import inspect
from pathlib import Path
import sys
import types

import profile_class_reads as meta
import profile_semantics as base


flags = set()
channels = set()
constructors = {}
slots = {}


def qualified(cls):
    return cls.__module__ + "." + cls.__qualname__


class Flag:
    def __init__(self, site, value):
        self.site, self.value = site, value

    def __get__(self, obj, owner=None):
        base.profile.hit(self.site, "direct")
        return self.value


class Slot:
    def __init__(self, descriptor, channel):
        self.descriptor, self.channel = descriptor, channel

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        base.profile.hit(self.channel, "used")
        return self.descriptor.__get__(obj, owner)

    def __set__(self, obj, value):
        self.descriptor.__set__(obj, value)

    def __delete__(self, obj):
        self.descriptor.__delete__(obj)


def install(names):
    def decorate(cls):
        if type(cls) is type and cls.__getattribute__ is object.__getattribute__:
            for name in set(names) & flags:
                value = vars(cls).get(name)
                if type(value) is bool:
                    setattr(cls,name,Flag(base.profile.site(f"forward:{qualified(cls)}:{name}"),value))
        return cls
    return decorate


def sink(fields):
    def decorate(cls):
        if (type(cls) is not type or cls.__getattribute__ is not object.__getattribute__
            or cls.__setattr__ is not object.__setattr__ or cls.__new__ is not object.__new__):
            return cls
        for param,field in fields:
            member = vars(cls).get(field)
            if type(member) is types.MemberDescriptorType:
                label = "slot-use:" + qualified(cls) + ":" + field
                wrapper = Slot(member,base.profile.site(label))
                setattr(cls,field,wrapper)
                constructors[cls,param] = (label,wrapper)
                slots[cls,field] = wrapper
        return cls
    return decorate


def transfer(cls, param, obj, name):
    owner = obj if isinstance(obj,type) else type(obj)
    flag = inspect.getattr_static(owner,name,None)
    dest = constructors.get((cls,param)) if type(cls) is type else None
    if type(flag) is Flag and dest:
        # A shadowing instance value does not depend on the class default.
        # A nonstandard lookup or a replaced slot requires the direct read.
        if (not isinstance(obj,type) and type(obj).__getattribute__ is object.__getattribute__
            and (not getattr(type(obj),"__dictoffset__",0) or name not in vars(obj))):
            label,wrapper = dest
            if vars(cls).get(wrapper.descriptor.__name__) is wrapper:
                base.profile.hit(flag.site, label)
                return flag.value
    return getattr(obj,name)


def capture(obj, field):
    wrapper = slots.get((type(obj),field))
    if wrapper is not None:
        return wrapper.descriptor.__get__(obj,type(obj)), wrapper.channel
    return getattr(obj,field), None


def use(value, channel):
    if channel is not None:
        base.profile.hit(channel,"used")
    return value


def plan_sinks(cls):
    fn = next((s for s in cls.body if isinstance(s,ast.FunctionDef) and s.name=="__init__"),None)
    if not fn or fn.decorator_list or not fn.args.args: return []
    receiver=fn.args.args[0].arg
    result=[]
    for s in fn.body:
        if (isinstance(s,ast.Assign) and len(s.targets)==1 and isinstance(s.targets[0],ast.Attribute)
            and isinstance(s.targets[0].value,ast.Name) and s.targets[0].value.id==receiver and isinstance(s.value,ast.Name)
            and s.targets[0].attr in channels and s.value.id in {a.arg for a in fn.args.args + fn.args.kwonlyargs}
            and sum(isinstance(n,ast.Name) and n.id==s.value.id for n in ast.walk(fn))==1
            and not any(isinstance(n,ast.Name) and n.id in {"vars","locals","eval","exec"} for n in ast.walk(fn))):
            result.append((s.value.id,s.targets[0].attr))
    return result


class Transform(meta.Transform):
    def visit_ClassDef(self,node):
        plans=plan_sinks(node) if not node.decorator_list else []
        super().visit_ClassDef(node)
        if plans:
            node.decorator_list.insert(0,self.wrap(node,"_rwx_forward_sink",[ast.Constant(tuple(plans))]))
        return node

    def visit_Call(self,node):
        if isinstance(node.func,ast.Name) and not any(isinstance(a,ast.Starred) for a in node.args):
            for kw in node.keywords:
                ref=kw.value
                if kw.arg in channels and isinstance(ref,ast.Attribute) and ref.attr in flags:
                    kw.value=self.wrap(ref,"_rwx_forward",[ast.Name(node.func.id,ast.Load()),ast.Constant(kw.arg),ref.value,ast.Constant(ref.attr)])
        return super().visit_Call(node)

    def visit_FunctionDef(self,fn):
        nodes=list(ast.walk(fn))
        if (any(isinstance(n,(ast.Lambda,ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and n is not fn for n in nodes)
            or any(isinstance(n,ast.Name) and n.id in {"vars","locals","exec","eval"} for n in nodes)):
            return self.generic_visit(fn)
        for s in fn.body:
            if not (isinstance(s,ast.Assign) and len(s.targets)==1 and isinstance(s.targets[0],ast.Name)
                    and isinstance(s.value,ast.Attribute) and s.value.attr in channels):
                continue
            var=s.targets[0].id
            if sum(isinstance(n,ast.Name) and n.id==var and not isinstance(n.ctx,ast.Load) for n in nodes)!=1:
                continue
            chan="_rwx_read_"+var
            if any(isinstance(n,ast.Name) and n.id==chan for n in nodes): continue
            for parent in nodes:
                for attr,child in ast.iter_fields(parent):
                    def wrapped(v):
                        if isinstance(v,ast.Name) and isinstance(v.ctx,ast.Load) and v.id==var:
                            return self.wrap(v,"_rwx_use_slot",[v,ast.Name(chan,ast.Load())])
                        return v
                    if isinstance(child,list):
                        setattr(parent,attr,[wrapped(v) for v in child])
                    elif isinstance(child,ast.Name):
                        setattr(parent,attr,wrapped(child))
            ref=s.value
            s.targets=[ast.Tuple([s.targets[0],ast.Name(chan,ast.Store())],ast.Store())]
            s.value=self.wrap(ref,"_rwx_capture_slot",[ref.value,ast.Constant(ref.attr)])
        return self.generic_visit(fn)


if __name__=="__main__":
    for p in (Path(sys.argv[1])/"sqlglot").rglob("*.py"):
        for n in ast.walk(ast.parse(p.read_bytes())):
            if isinstance(n,ast.Call) and isinstance(n.func,ast.Name):
                for k in n.keywords:
                    if k.arg and isinstance(k.value,ast.Attribute) and k.value.attr.isupper() and k.arg==k.value.attr.lower():
                        flags.add(k.value.attr)
                        channels.add(k.arg)
    builtins._rwx_forward=transfer
    builtins._rwx_forward_sink=sink
    builtins._rwx_capture_slot=capture
    builtins._rwx_use_slot=use
    builtins._rwx_scalars=install
    builtins._rwx_reflected=lambda obj:meta.namespace(obj,"all")
    builtins._rwx_ns=meta.namespace
    base.Transform=Transform
    base.Profile.entry=lambda self,code,offset:sys.monitoring.DISABLE
    base.Profile.audit=lambda self,event,args:None
    base.main()
