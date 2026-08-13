"""Executors — WHERE a runner runs. Same image, same code, different machine.

    Runner    what runs   (stages + contracts — framework.py)
    Executor  where it runs (this file)

The separation is the point: a pipeline verified locally is the SAME pipeline that runs on
a pod. Nothing is re-implemented for the cloud, so nothing can drift between them.

    LocalExecutor    docker compose, bind mounts, corpus already on disk
    RunPodExecutor   same Dockerfile, data via S3, results via S3

## Choosing

`select()` uses the measured runtime estimate, not judgement:

    <= 15 min CPU  -> LocalExecutor
    >  15 min CPU  -> RunPodExecutor

That threshold is a stated preference ("nothing over 15 minutes on the laptop"), so it lives
in one constant rather than in someone's memory.

## What the RunPod path assumes

  * the image is built for linux/amd64 — RunPod is x86, a laptop is arm64
  * corpus goes up via S3 and results come back via S3, so a spot pod can die without
    losing work
  * audio never comes back down: it was already uploaded
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Protocol

LOCAL_BUDGET_MINUTES = 15.0


@dataclass
class ExecResult:
    ok: bool
    where: str
    command: str = ""
    seconds: float = 0.0
    stdout_tail: str = ""
    error: str | None = None
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


class Executor(Protocol):
    name: str

    def describe(self) -> dict: ...
    def preflight(self) -> dict: ...
    def prepare(self, source: str) -> ExecResult: ...
    def execute(self, stage: str, source: str, **kw) -> ExecResult: ...
    def collect(self) -> ExecResult: ...


# --------------------------------------------------------------------------------------
# Local
# --------------------------------------------------------------------------------------

class LocalExecutor:
    """docker compose on this machine. Corpus is already bind-mounted; nothing to move."""

    name = "local"

    def __init__(self, *, service: str = "asr", repo: Path | None = None,
                 compose_file: str = "docker-compose.yml"):
        self.service = service
        self.repo = Path(repo or Path(__file__).resolve().parent.parent)
        self.compose_file = compose_file

    def describe(self) -> dict:
        return {"executor": self.name, "service": self.service, "repo": str(self.repo),
                "data_transfer": "none — bind mounts", "arch": "host native"}

    def preflight(self) -> dict:
        checks: dict[str, Any] = {}
        checks["docker"] = shutil.which("docker") is not None
        try:
            out = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                                 capture_output=True, text=True, timeout=15)
            checks["daemon"] = out.returncode == 0
            checks["server_version"] = out.stdout.strip()
        except Exception as exc:
            checks["daemon"] = False
            checks["error"] = str(exc)[:120]
        checks["compose_file"] = (self.repo / self.compose_file).exists()
        checks["ok"] = bool(checks.get("docker") and checks.get("daemon")
                            and checks.get("compose_file"))
        return checks

    def prepare(self, source: str) -> ExecResult:
        return ExecResult(True, self.name, detail={"note": "bind-mounted; nothing to move"})

    def _compose(self, args: list[str], timeout: int) -> ExecResult:
        cmd = ["docker", "compose"]
        if self.service != "pipeline":
            cmd += ["--profile", self.service]
        cmd += ["run", "--rm", "--entrypoint", "python", self.service] + args
        import time
        t0 = time.time()
        try:
            out = subprocess.run(cmd, cwd=self.repo, capture_output=True, text=True,
                                 timeout=timeout)
        except subprocess.TimeoutExpired:
            return ExecResult(False, self.name, " ".join(cmd), error=f"timed out ({timeout}s)")
        tail = "\n".join((out.stdout or "").strip().splitlines()[-14:])
        return ExecResult(out.returncode == 0, self.name, " ".join(cmd),
                          round(time.time() - t0, 1), tail,
                          None if out.returncode == 0
                          else "\n".join((out.stderr or "").splitlines()[-6:]))

    def execute(self, stage: str, source: str, *, limit: int | None = None,
                timeout: int = 7200) -> ExecResult:
        args = ["-m", "runners.batch", stage, "--source", source]
        if limit:
            args += ["--limit", str(limit)]
        return self._compose(args, timeout)

    def collect(self) -> ExecResult:
        return ExecResult(True, self.name,
                          detail={"note": "results already in ./out via bind mount"})


# --------------------------------------------------------------------------------------
# RunPod
# --------------------------------------------------------------------------------------

class RunPodExecutor:
    """Same image on a pod. Data moves through S3, so a dead pod loses nothing."""

    name = "runpod"

    def __init__(self, *, ssh_host: str | None = None, ssh_port: int = 22,
                 ssh_key: str | None = None, workspace: str = "/workspace",
                 image: str = "linguabackend/gpu:0.1.0",
                 storage: Any = None, repo: Path | None = None):
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.ssh_key = ssh_key
        self.workspace = workspace
        self.image = image
        self.repo = Path(repo or Path(__file__).resolve().parent.parent)
        self._storage = storage

    @property
    def storage(self):
        if self._storage is None:
            import sys
            sys.path.insert(0, str(self.repo))
            from .storage import Storage
            self._storage = Storage()
        return self._storage

    def describe(self) -> dict:
        return {"executor": self.name, "host": self.ssh_host or "<unset>",
                "workspace": self.workspace, "image": self.image,
                "data_transfer": "S3 (survives pod death)",
                "arch": "linux/amd64 — build with --platform"}

    def preflight(self) -> dict:
        checks: dict[str, Any] = {"host_configured": bool(self.ssh_host)}
        s3 = self.storage.check() if self.storage.available else {"ok": False,
                                                                  "error": "no credentials"}
        checks["s3"] = {k: v for k, v in s3.items() if k != "objects_sampled"}
        checks["image_built_amd64"] = self._image_is_amd64()
        if self.ssh_host:
            checks["ssh"] = self._ssh_ok()
        checks["ok"] = bool(s3.get("ok") and checks.get("image_built_amd64"))
        if not checks["ok"]:
            checks["blocking"] = [
                k for k, v in (("s3", s3.get("ok")),
                               ("image_built_amd64", checks["image_built_amd64"]))
                if not v]
        return checks

    def _image_is_amd64(self) -> bool:
        try:
            out = subprocess.run(
                ["docker", "image", "inspect", self.image, "--format", "{{.Architecture}}"],
                capture_output=True, text=True, timeout=20)
            return out.returncode == 0 and out.stdout.strip() == "amd64"
        except Exception:
            return False

    def _ssh(self, script: str, timeout: int = 3600) -> ExecResult:
        if not self.ssh_host:
            return ExecResult(False, self.name, error="ssh_host not configured")
        cmd = ["ssh", "-p", str(self.ssh_port)]
        if self.ssh_key:
            cmd += ["-i", self.ssh_key]
        cmd += ["-o", "StrictHostKeyChecking=accept-new", self.ssh_host, "bash", "-s"]
        import time
        t0 = time.time()
        try:
            out = subprocess.run(cmd, input=script, capture_output=True, text=True,
                                 timeout=timeout)
        except subprocess.TimeoutExpired:
            return ExecResult(False, self.name, error=f"ssh timed out ({timeout}s)")
        return ExecResult(out.returncode == 0, self.name, "ssh",
                          round(time.time() - t0, 1),
                          "\n".join((out.stdout or "").splitlines()[-14:]),
                          None if out.returncode == 0
                          else "\n".join((out.stderr or "").splitlines()[-6:]))

    def _ssh_ok(self) -> bool:
        return self._ssh("echo ok; nvidia-smi -L 2>/dev/null | head -1", timeout=60).ok

    def prepare(self, source: str) -> ExecResult:
        """Push the source's audio to S3. Idempotent — re-running re-uploads only what
        is not already there once a listing check is added; for now it is a plain put."""
        raw = Path(self.repo.parent) / "corpus_data" / "raw" / source
        if not raw.exists():
            return ExecResult(False, self.name, error=f"no local source at {raw}")
        r = self.storage.upload_dir(raw, f"corpus/raw/{source}")
        return ExecResult(bool(r.get("ok")), self.name,
                          detail=r, error=r.get("error"))

    def execute(self, stage: str, source: str, *, limit: int | None = None,
                timeout: int = 14400) -> ExecResult:
        lim = f" --limit {limit}" if limit else ""
        # NO `docker build` and NO `docker run` here. A RunPod pod IS a container: there is
        # no nested Docker daemon, no --privileged in their API, no Docker socket.
        #   "Runpod Pods use custom Docker images, so you can't directly build Docker
        #    containers or use Docker Compose on a GPU Pod."
        #   https://docs.runpod.io/tutorials/pods/build-docker-images
        # The pod's own image IS the environment, chosen at creation time. We just run the
        # code inside it. Earlier versions of this file shelled out to `docker build`, which
        # could never have worked.
        script = f"""
set -e
cd {self.workspace}/code 2>/dev/null || {{ echo "code not synced to pod"; exit 1; }}
python -m runners.batch {stage} --source {source}{lim}
"""
        return self._ssh(script, timeout=timeout)

    def collect(self, prefix: str = "out/") -> ExecResult:
        r = self.storage.download_prefix(prefix, self.repo / "out")
        return ExecResult(bool(r.get("ok")), self.name, detail=r, error=r.get("error"))


# --------------------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------------------

def select(estimated_cpu_minutes: float, *, force: str | None = None,
           **kw) -> Executor:
    """Pick an executor from the measured estimate, not from judgement."""
    if force == "local":
        return LocalExecutor(**{k: v for k, v in kw.items()
                                if k in ("service", "repo", "compose_file")})
    if force == "runpod":
        return RunPodExecutor(**{k: v for k, v in kw.items()
                                 if k not in ("service", "compose_file")})
    if estimated_cpu_minutes <= LOCAL_BUDGET_MINUTES:
        return LocalExecutor(**{k: v for k, v in kw.items()
                                if k in ("service", "repo", "compose_file")})
    return RunPodExecutor(**{k: v for k, v in kw.items()
                             if k not in ("service", "compose_file")})


def selftest() -> dict:
    """Verify executor selection and preflight without touching a pod."""
    cases = {}
    cases["under_budget_picks_local"] = select(5.0).name == "local"
    cases["over_budget_picks_runpod"] = select(45.0).name == "runpod"
    cases["force_overrides"] = select(45.0, force="local").name == "local"

    local = LocalExecutor()
    pf = local.preflight()
    cases["local_preflight_runs"] = "ok" in pf
    cases["local_needs_no_transfer"] = local.prepare("anything").ok is True

    rp = RunPodExecutor()
    d = rp.describe()
    cases["runpod_declares_amd64"] = "amd64" in d["arch"]
    cases["runpod_without_host_fails_cleanly"] = (
        rp._ssh("echo hi").ok is False)
    return {"passed": all(cases.values()), "cases": cases,
            "local_preflight": pf, "runpod_describe": d}
