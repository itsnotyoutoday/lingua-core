"""A fake workload: the minimum a real one must provide — Stage subclasses + a registry."""
import os, time
from lingua_core.framework import Stage

MARK = os.environ.get("FAKE_MARK", "/tmp/fakewl")

class Acquire(Stage):
    name, number = "acquire", 1
    produces = ("sources",)
    def execute(self, ctx):
        ctx.put("sources", [f"f{i}.wav" for i in range(5)])
        return {"n": 5}
    def verify_outputs(self, ctx):
        from lingua_core.framework import Verification
        ok = os.path.exists(f"{MARK}/acquire.done")
        return Verification(ok=ok, checks={"marker": ok},
                            failures=[] if ok else ["acquire artifacts missing on disk"])

class Normalize(Stage):
    name, number = "normalize", 2
    requires, produces = ("sources",), ("normalized",)
    def execute(self, ctx):
        srcs = ctx.get("sources")
        for i, s in enumerate(srcs):
            ctx.progress(i + 1, len(srcs), note=s)
            time.sleep(0.01)
        ctx.put("normalized", srcs)
        return {"n": len(srcs)}
    def verify_outputs(self, ctx):
        from lingua_core.framework import Verification
        ok = os.path.exists(f"{MARK}/normalize.done")
        return Verification(ok=ok, checks={"marker": ok},
                            failures=[] if ok else ["normalized audio not on disk"])

class Measure(Stage):
    name, number = "measure", 3
    requires, produces = ("normalized",), ("measurements",)
    def execute(self, ctx):
        ctx.put("measurements", {"x": 1})
        return {"ok": True}

class Liar(Stage):
    """Reports success, produces nothing — the bug class the framework exists to catch."""
    name, number = "liar", 4
    produces = ("never_made",)
    def execute(self, ctx):
        return {"claimed": 40}

STAGES = {"acquire": Acquire, "normalize": Normalize, "measure": Measure, "liar": Liar}
