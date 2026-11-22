"""Identify module initializers responsible for shared collection-time settings reads."""
import builtins
import sys

import profile_cold_reads as cold
import profile_class_reads as meta
import profile_semantics as base


class Origins(base.Profile):
    def __init__(self, root, ids):
        super().__init__(root, ids)
        self.names = {}

    def site(self, key):
        index = super().site(key)
        self.names[index] = key
        return index

    def hit(self, site, value):
        if self.mask != self.startup or value == "installed":
            return
        name = self.names[site]
        frame = sys._getframe(1)
        while frame:
            if frame.f_code.co_name == "<module>":
                path = self.relative(frame.f_code.co_filename)
                if path and path.startswith(("sqlglot/", "tests/")):
                    break
            frame = frame.f_back
        origin = path if frame else "unknown"
        super().hit(self.site("origin:" + name), value + "|" + origin)


if __name__ == "__main__":
    builtins._rwx_scalars = cold.install
    builtins._rwx_reflected = cold.reflect
    builtins._rwx_ns = meta.namespace
    builtins._rwx_cold_attr = lambda obj,name: base.profile.attribute(obj,name)
    base.Profile = Origins
    base.Transform = cold.Transform
    base.Profile.entry = lambda self, code, offset: sys.monitoring.DISABLE
    base.Profile.audit = lambda self, event, args: None
    base.main()
