"""Validate specs against the harness contract — the loader's half of the agreement.

## Why this exists instead of a shared import

The loader and the harness share no code. They agree on `contract.json`, and each proves
it honours that file independently: the harness asserts it implements the endpoints, env
vars and state vocabulary it advertises; this module asserts every spec the loader emits
conforms before a pod is created.

A shared package would have been the obvious alternative, and it is worse. It does not
actually guarantee agreement — a `pip install …@main` once served a stale engine out of a
Docker layer cache, so both sides "shared" a module while running different versions of it
— and it forces a dependency edge that this architecture deliberately does not have.

## Why the LIVE contract beats a vendored copy

`from_image()` fetches the contract from the running harness at `/v1/contract`, so a spec
is validated against the interface of the exact image about to run it. A file checked into
this repo would be a second copy, and second copies are what drifted three times in one
day. The bundled copy is a fallback for offline use and is labelled as such.

## Why validation happens here at all

Because it is free here and expensive later. A typo'd stage name caught on a laptop costs
a millisecond; the same typo caught in-pod costs the image pull, the boot, and the minutes
until someone notices — which today was thirteen of them at $0.74/hr.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

CONTRACT_URL_PATH = "/v1/contract"

#: RunPod's proxy sits behind Cloudflare, which rejects urllib's default agent with
#: `error code: 1010` — a 403 that looks like an auth failure and is not. Cost an hour once.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class ContractError(RuntimeError):
    """A spec that would fail on the pod, refused before the pod exists."""


def from_image(base_url: str, token: str = "", timeout: int = 20) -> dict:
    """Fetch the contract from a running harness. The authoritative source."""
    req = urllib.request.Request(base_url.rstrip("/") + CONTRACT_URL_PATH,
                                 headers={"User-Agent": _UA})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def bundled() -> dict:
    """A local copy, for validating offline before anything is provisioned.

    Deliberately searched for rather than vendored into this package: if it is a sibling
    checkout of pod-harness, that is the real file, not a copy of it.
    """
    here = Path(__file__).resolve()
    repo = here.parent.parent.parent            # src/pod_loader/contract.py -> repo root
    for c in (here.parent / "contract.json",    # vendored beside this module
              repo / "contract.json",           # dropped in the repo root
              repo.parent / "pod-harness" / "contract.json",   # sibling checkout
              Path(os.environ.get("POD_HARNESS_CONTRACT", "/nonexistent"))):
        if c.is_file():
            return json.loads(c.read_text())
    raise ContractError(
        "no contract available offline.\n"
        "  Either check out pod-harness beside this repo, or validate against a running "
        "harness with from_image().")


# -- validation ------------------------------------------------------------------------
#
# A deliberately small subset of JSON Schema, implemented here rather than pulled in as a
# dependency. The loader's dependency list is a security surface for a tool that holds
# cloud credentials, and the spec schema uses six keywords.

def _check(node: Any, schema: dict, path: str, out: list[str]) -> None:
    t = schema.get("type")
    types = [t] if isinstance(t, str) else (t or [])
    if types:
        ok = any(
            (ty == "object" and isinstance(node, dict))
            or (ty == "array" and isinstance(node, list))
            or (ty == "string" and isinstance(node, str))
            or (ty == "integer" and isinstance(node, int) and not isinstance(node, bool))
            or (ty == "number" and isinstance(node, (int, float)) and not isinstance(node, bool))
            or (ty == "boolean" and isinstance(node, bool))
            or (ty == "null" and node is None)
            for ty in types)
        if not ok:
            out.append(f"{path}: expected {'/'.join(types)}, got {type(node).__name__}")
            return

    if "const" in schema and node != schema["const"]:
        out.append(f"{path}: must be {schema['const']!r}, got {node!r}")
    if "enum" in schema and node not in schema["enum"]:
        out.append(f"{path}: must be one of {schema['enum']}, got {node!r}")
    if "pattern" in schema and isinstance(node, str) and not re.match(schema["pattern"], node):
        out.append(f"{path}: {node!r} does not match {schema['pattern']}")
    if "minItems" in schema and isinstance(node, list) and len(node) < schema["minItems"]:
        out.append(f"{path}: needs at least {schema['minItems']} item(s), got {len(node)}")
    if "exclusiveMinimum" in schema and isinstance(node, (int, float)) \
            and node <= schema["exclusiveMinimum"]:
        out.append(f"{path}: must be greater than {schema['exclusiveMinimum']}, got {node}")

    if isinstance(node, dict):
        for r in schema.get("required", []):
            if r not in node:
                out.append(f"{path}: missing required field {r!r}")
        for k, sub in (schema.get("properties") or {}).items():
            if k in node and isinstance(sub, dict):
                _check(node[k], sub, f"{path}.{k}" if path else k, out)
    if isinstance(node, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(node):
            _check(item, schema["items"], f"{path}[{i}]", out)


def validate_spec(spec: dict, contract: dict | None = None) -> list[str]:
    """Return a list of problems. Empty means the harness will accept this spec."""
    c = contract or bundled()
    problems: list[str] = []
    _check(spec, c["spec"], "", problems)
    return problems


def require_valid(spec: dict, contract: dict | None = None) -> None:
    """Raise unless the spec conforms. Call before spending anything."""
    problems = validate_spec(spec, contract)
    if problems:
        raise ContractError(
            "this spec would be rejected by the harness:\n" +
            "\n".join(f"    {p}" for p in problems) +
            "\n  Caught here for free; on a pod this costs the image pull, the boot, and "
            "the minutes until someone notices.")


def required_env(contract: dict | None = None, *, batch: bool = True) -> dict:
    """Every variable the harness requires, so a launcher cannot forget one.

    Reading this from the contract rather than hardcoding it is the point: when the harness
    starts needing a new root, launches fail loudly at composition time instead of the pod
    refusing to boot.
    """
    c = contract or bundled()
    env = dict(c["env"]["required_always"])
    if batch:
        env.update(c["env"]["required_for_batch"])
    return env


def check_env(env: dict, contract: dict | None = None, *, batch: bool = True) -> list[str]:
    missing = [k for k in required_env(contract, batch=batch) if not env.get(k)]
    return [f"missing {k}: {required_env(contract, batch=batch)[k]}" for k in missing]


if __name__ == "__main__":                        # python -m pod_loader.contract <spec.json>
    import sys
    c = bundled()
    print(f"contract v{c['contract_version']}: "
          f"{len(c['api']['endpoints'])} endpoints, "
          f"{len(required_env(c))} required env vars")
    for f in sys.argv[1:]:
        problems = validate_spec(json.loads(Path(f).read_text()), c)
        print(f"  {f}: " + ("valid ✅" if not problems else f"{len(problems)} problem(s)"))
        for p in problems:
            print(f"      {p}")
    raise SystemExit(0)
