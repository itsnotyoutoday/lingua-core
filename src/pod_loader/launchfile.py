"""`.pod.env` — everything a launch directory needs to start a job, in one readable file.

## Why a file and not flags

A launch has a dozen inputs: which store, which credentials, which image, which volume,
what the budget is, what the job is, what extra environment the workload wants. Passing
those as flags means retyping them, and retyping them means getting one wrong on the
attempt that matters. Putting them in a file next to the job makes a launch reproducible
and reviewable — you can read it before spending money.

## Where it looks

`./.pod.env`, then upward to `$HOME`. So a workload repo carries its own launch config and
you can run from any subdirectory of it.

## What it can contain

    # ---- destination -------------------------------------------------------
    TARGET          = runpod | local          where to run
    IMAGE           = ghcr.io/…/pod-harness:latest
    FLAVORS         = cpu3c,cpu5c             shapes to try, in order
    CLOUDS          = SECURE,COMMUNITY
    BUDGET_MIN      = 60                      hard kill after this
    MAX_COST_HR     = 1.50                    refuse to launch above this

    # ---- storage -----------------------------------------------------------
    STORE           = runpod | cloudflare | aws | minio
    RUNPOD_VOLUME   = ~/runpod-volume.key     RunPod-only; pins the datacentre

    # ---- the job -----------------------------------------------------------
    JOB_SPEC        = jobs/benchmark.json
    WORKLOAD        = ../lingua-maintenance   published before launch
    AUTORUN         = true                    submit immediately, or just boot

    # ---- anything else the workload wants ----------------------------------
    ENV_LINGUA_MFA_ACOUSTIC = english_us_arpa

Keys prefixed `ENV_` are forwarded to the pod verbatim with the prefix stripped, so a
workload can pass its own settings without this file knowing what they mean.

## What it must NOT contain

Credentials. `STORE` names a profile and `RUNPOD_VOLUME` points at a key file; the secrets
stay in those files, outside any repo, where `*.key` already keeps them out of git. A
`.pod.env` with a secret in it is a `.pod.env` that gets committed.

The loader refuses to read a value that looks like a key rather than a path, because the
alternative is discovering it in a public repo later.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

FILENAMES = (".pod.env", "pod.env")

#: Values that must never appear inline. Matched loosely on purpose — a false positive
#: costs one confused moment; a false negative costs a leaked credential.
_SECRET_KEYS = re.compile(r"(SECRET|PASSWORD|API_KEY|ACCESS_KEY|TOKEN)$")


class LaunchFileError(RuntimeError):
    pass


def _find(start: Path | None = None) -> Path | None:
    d = (start or Path.cwd()).resolve()
    home = Path.home().resolve()
    while True:
        for n in FILENAMES:
            if (d / n).is_file():
                return d / n
        if d == home or d.parent == d:
            return None
        d = d.parent


def _parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip().upper()] = v.strip().strip("'\"")
    return out


@dataclass
class LaunchConfig:
    """A resolved launch. Everything needed to start a job, nothing secret."""

    target: str = "runpod"
    image: str = "ghcr.io/itsnotyoutoday/pod-harness:latest"
    flavors: tuple = ()
    clouds: tuple = ("SECURE", "COMMUNITY")
    budget_min: float = 60.0
    max_cost_hr: float = 0.0            # 0 = no ceiling
    vcpu: int = 8
    disk_gb: int = 30

    store: str = ""                     # profile name; "" = the default key file
    runpod_volume: str = ""             # path or inline id; RunPod only

    job_spec: str = ""
    workload: str = ""
    autorun: bool = True

    extra_env: dict = field(default_factory=dict)
    source: str = "(defaults)"

    def describe(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "extra_env"}
        d["extra_env_keys"] = sorted(self.extra_env)
        return d


def load(path: str | Path | None = None) -> LaunchConfig:
    """Read `.pod.env`, or return defaults when there is none."""
    p = Path(path) if path else _find()
    if not p or not p.is_file():
        if path:
            raise LaunchFileError(f"no launch file at {path}")
        return LaunchConfig()

    kv = _parse(p.read_text())

    # Refuse inline secrets before doing anything else with the file.
    leaked = [k for k, v in kv.items()
              if _SECRET_KEYS.search(k) and v and not Path(v).expanduser().exists()]
    if leaked:
        raise LaunchFileError(
            f"{p} appears to contain secrets inline: {', '.join(leaked)}\n"
            f"  Put credentials in a *.key file outside the repo and reference it by path.\n"
            f"  A launch file lives next to the job, which means it gets committed.")

    def g(key, default=""):
        return kv.get(key, default)

    def num(key, default):
        try:
            return type(default)(kv[key])
        except (KeyError, ValueError):
            return default

    cfg = LaunchConfig(
        target=g("TARGET", "runpod").lower(),
        image=g("IMAGE") or LaunchConfig.image,
        flavors=tuple(x.strip() for x in g("FLAVORS").split(",") if x.strip()),
        clouds=tuple(x.strip().upper() for x in g("CLOUDS").split(",") if x.strip())
                or LaunchConfig.clouds,
        budget_min=num("BUDGET_MIN", 60.0),
        max_cost_hr=num("MAX_COST_HR", 0.0),
        vcpu=num("VCPU", 8),
        disk_gb=num("DISK_GB", 30),
        store=g("STORE").lower(),
        runpod_volume=g("RUNPOD_VOLUME"),
        job_spec=g("JOB_SPEC"),
        workload=g("WORKLOAD"),
        autorun=g("AUTORUN", "true").lower() in ("1", "true", "yes"),
        extra_env={k[4:]: v for k, v in kv.items() if k.startswith("ENV_")},
        source=str(p),
    )
    return cfg


def apply_to_environ(cfg: LaunchConfig) -> None:
    """Export what the loader's own libraries read from the environment.

    Only the store profile and the volume: everything else is passed explicitly at launch,
    because mutating the environment to communicate between our own functions is how
    configuration becomes untraceable.
    """
    if cfg.store:
        os.environ["PODH_S3_PROFILE"] = cfg.store
    if cfg.runpod_volume:
        os.environ["RUNPOD_VOLUME"] = str(Path(cfg.runpod_volume).expanduser())


def pod_env(cfg: LaunchConfig, *, job_id: str, spec_key: str, code_root: str = "",
            workspace: str = "/workspace") -> dict:
    """Compose the environment the pod is given.

    Built here rather than at each call site so that a new required variable is added in
    one place — and checked against the harness contract, so a missing one fails while
    composing instead of when the pod refuses to boot.
    """
    env = {
        "PODH_MODE": "batch" if cfg.autorun else "serve",
        "PODH_WORKSPACE": workspace,
        "PODH_JOB_ID": job_id,
        "PODH_JOB_SPEC": f"{workspace}/{spec_key}",
        "PODH_LOG_ROOT": f"{workspace}/runs",
        "PODH_RUN_PREFIX": f"runs/{job_id}",
        "PODH_WRITE_PREFIXES": f"runs/{job_id},_tmp/",
        "PODH_MOUNT_KIND": "volume" if cfg.runpod_volume else "object",
        "PODH_MAX_LIFE_SEC": str(int(cfg.budget_min * 60)),
        "PODH_MAX_IDLE_SEC": "0",
    }
    if code_root:
        env["PODH_CODE_ROOT"] = f"{workspace}/{code_root}"
    env.update(cfg.extra_env)          # workload settings, forwarded verbatim
    return env


def check(cfg: LaunchConfig) -> list[str]:
    """Problems that would waste a launch. Cheap, and run before anything is provisioned."""
    problems = []
    if cfg.target not in ("runpod", "local"):
        problems.append(f"TARGET={cfg.target!r} is not runpod or local")
    if cfg.job_spec and not Path(cfg.job_spec).expanduser().exists():
        problems.append(f"JOB_SPEC not found: {cfg.job_spec}")
    if cfg.workload and not (Path(cfg.workload).expanduser() / "code").is_dir():
        problems.append(f"WORKLOAD has no code/ directory: {cfg.workload}")
    if cfg.budget_min <= 0:
        problems.append("BUDGET_MIN must be positive — it is the only hard cost ceiling")
    if cfg.store:
        try:
            from .objectstore import resolve_config
            resolve_config(cfg.store)
        except Exception as e:
            problems.append(f"STORE={cfg.store!r}: {str(e).splitlines()[0]}")
    return problems


def template() -> str:
    return """# How this job launches. Committed alongside the job; NEVER contains secrets.

TARGET        = runpod
IMAGE         = ghcr.io/itsnotyoutoday/pod-harness:latest
BUDGET_MIN    = 60
MAX_COST_HR   = 1.50

# Store profile. Credentials live in a *.key file outside the repo.
#   runpod      RunPod's endpoint — NOT true S3 (no presigned URLs, batch delete 307s)
#   cloudflare  S3-compatible, verified
STORE         = runpod

# RunPod network volume. RunPod-only, and it PINS compute to one datacentre.
RUNPOD_VOLUME = ~/runpod-volume.key

JOB_SPEC      = jobs/my-job.json
WORKLOAD      = ../my-workload
AUTORUN       = true

# Forwarded to the pod with ENV_ stripped:
# ENV_LINGUA_MFA_ACOUSTIC = english_us_arpa
"""


if __name__ == "__main__":                      # python -m pod_loader.launchfile
    import json
    import sys
    if "--template" in sys.argv:
        print(template())
        raise SystemExit(0)
    try:
        cfg = load()
    except LaunchFileError as e:
        print(f"error: {e}")
        raise SystemExit(1)
    print(json.dumps(cfg.describe(), indent=2))
    problems = check(cfg)
    print("\nchecks: " + ("all pass" if not problems else ""))
    for p in problems:
        print(f"  ✗ {p}")
    raise SystemExit(1 if problems else 0)
