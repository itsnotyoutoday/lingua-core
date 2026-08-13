"""Publish a workload's stage manifest — stage knowledge without importing the workload.

    python -m lingua_core.capabilities trainer.stages:STAGES -o code/capabilities.json

## Why this exists

The engine loads stage implementations by importing them, which requires the workload's
code and all its dependencies — MFA, torch, librosa. That is fine inside a pod and useless
anywhere else. A laptop deciding whether to spend a pod-hour cannot import a stack it does
not have installed.

So CI introspects the registry once, at publish time, and writes the result next to the
code. Then:

  * `/v1/` discovery reports the real stage vocabulary for whatever code a pod carries
  * the control plane rejects a typo'd stage name or an unwired pipeline BEFORE launching
  * `dry_run` is meaningful off-pod

This is what replaces the old hardcoded `STAGE_CLASSES` import in the framework: the same
information, but sourced from the workload that owns it rather than compiled into the runner.

## What it does not capture

Readiness. Whether `align`'s inputs actually exist on this mount is a question only the pod
can answer, via `Runner.plan()`. The manifest supports the SHALLOW check — names and wiring
— and says so rather than implying more.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def introspect(registry: dict[str, Any]) -> list[dict]:
    """Read the declared contract off each Stage class. No instantiation, no execution."""
    rows = []
    for name, cls in registry.items():
        rows.append({
            "name": name,
            "number": getattr(cls, "number", 0),
            "requires": list(getattr(cls, "requires", ()) or ()),
            "produces": list(getattr(cls, "produces", ()) or ()),
            "optional": bool(getattr(cls, "optional", False)),
            "doc": (getattr(cls, "__doc__", "") or "").strip().split("\n")[0][:160],
        })
    rows.sort(key=lambda r: (r["number"], r["name"]))
    return rows


def build(stages_from: str, *, rev: str = "", given: list[str] | None = None) -> dict:
    module_path, _, attr = stages_from.partition(":")
    if not attr:
        raise SystemExit(f"expected 'module:ATTR', got {stages_from!r}")
    try:
        mod = importlib.import_module(module_path)
    except Exception as exc:
        raise SystemExit(
            f"cannot import {module_path!r}: {type(exc).__name__}: {exc}\n"
            f"  sys.path[:4]={sys.path[:4]}\n"
            f"  hint: run this from the code root, or set PYTHONPATH to it") from None
    registry = getattr(mod, attr, None)
    if not isinstance(registry, dict):
        raise SystemExit(f"{stages_from} is not a dict of {{name: StageClass}}")

    return {
        "stages_from": stages_from,
        "stages": introspect(registry),
        # Artifacts a job may be handed without a stage producing them — a spec that points
        # at a corpus already on the mount supplies `sources` without running `acquire`, and
        # the wiring check must not call that broken.
        "given": given or ["sources"],
        "rev": rev,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "schema": 1,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("stages_from", help="module:ATTR, e.g. trainer.stages:STAGES")
    ap.add_argument("-o", "--out", default="capabilities.json")
    ap.add_argument("--rev", default="", help="the commit this code came from")
    ap.add_argument("--given", nargs="*", default=None,
                    help="artifacts supplied up front (default: sources)")
    a = ap.parse_args()

    caps = build(a.stages_from, rev=a.rev, given=a.given)
    Path(a.out).write_text(json.dumps(caps, indent=2), encoding="utf-8")
    print(f"{a.out}: {len(caps['stages'])} stages from {a.stages_from}")
    for s in caps["stages"]:
        print(f"  {s['number']:>2} {s['name']:<14} "
              f"requires={s['requires'] or '-'} produces={s['produces'] or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
