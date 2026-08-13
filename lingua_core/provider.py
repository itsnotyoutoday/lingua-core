"""Providers — the runner LIFECYCLE, independent of where it runs.

One interface, several backends. A job written for one runs unchanged on another, because
the instruction file describes WHAT to do and the provider decides WHERE.

    create()    bring a runner into existence      -> handle
    status()    is it ready? what is it doing?     -> RunnerStatus
    mount()     attach storage (local dir or S3)
    submit()    give it a job spec (JSON)
    poll()      progress while it works
    fetch()     retrieve results
    shutdown()  stop it
    destroy()   stop AND delete it

    LocalProvider    docker on this machine
    RunPodProvider   a pod, same image, data through S3

Adding a third (Lambda Labs, Vast, a bare server) means implementing this interface —
nothing above it changes.

## The instruction file is the contract

A job spec names sources and stages with paths RELATIVE to the mount root, so the same file
works whether the root is a local directory or an S3 prefix:

    {
      "job_id": "neutro_smoke",
      "region": "_neutro",
      "mount": {"kind": "local|s3", "root": "corpus/"},
      "sources": [{"id": "...", "path": "raw/openslr_108_mediaspeech_es/ES/ES",
                   "limit": 3}],
      "stages": ["normalize", "measure", "profile"],
      "opts": {"pool": "shape"}
    }

Nothing in it is provider-specific. That is what makes the local run a real rehearsal.
"""
from __future__ import annotations

import json
import subprocess
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

REPO = Path(__file__).resolve().parent.parent
STATE_DIR = REPO / "out" / "_reports" / "runners"

STATES = ("absent", "creating", "ready", "running", "done", "failed", "stopped")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _storage():
    """Lazy Storage import — providers must be usable with no S3 credentials present."""
    import sys
    sys.path.insert(0, str(REPO))
    from .storage import Storage
    return Storage()


# --------------------------------------------------------------------------------------
# Job specification
# --------------------------------------------------------------------------------------

@dataclass
class JobSpec:
    job_id: str
    region: str
    stages: list[str]
    sources: list[dict] = field(default_factory=list)
    mount: dict = field(default_factory=lambda: {"kind": "local", "root": "corpus/"})
    opts: dict = field(default_factory=dict)
    base: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls, path: str | Path) -> "JobSpec":
        d = json.loads(Path(path).read_text())
        return cls(job_id=d.get("job_id") or Path(path).stem,
                   region=d["region"], stages=d.get("stages") or [],
                   sources=d.get("sources") or [], base=d.get("base"),
                   mount=d.get("mount") or {"kind": "local", "root": "corpus/"},
                   opts=d.get("opts") or {})

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.as_dict(), indent=2))
        return p

    def validate(self) -> list[str]:
        problems = []
        if not self.region:
            problems.append("region is required")
        if not self.stages:
            problems.append("no stages listed — nothing to do")
        if self.mount.get("kind") not in ("local", "s3"):
            problems.append(f"mount.kind must be local|s3, got {self.mount.get('kind')!r}")
        for s in self.sources:
            if not s.get("id"):
                problems.append(f"source missing id: {s}")
        return problems


@dataclass
class RunnerStatus:
    runner_id: str
    provider: str
    state: str
    created: str = ""
    updated: str = ""
    job_id: str | None = None
    progress: dict = field(default_factory=dict)
    message: str = ""
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


class Provider(Protocol):
    kind: str

    def create(self, **kw) -> RunnerStatus: ...
    def status(self) -> RunnerStatus: ...
    def mount(self, spec: dict) -> dict: ...
    def push(self, source: str, *, dry_run: bool = False) -> dict: ...
    def submit(self, job: JobSpec) -> RunnerStatus: ...
    def poll(self) -> RunnerStatus: ...
    def fetch(self, dest: Path | None = None) -> dict: ...
    def shutdown(self) -> RunnerStatus: ...
    def destroy(self) -> RunnerStatus: ...


class BaseProvider:
    """State persistence shared by every provider — survives CLI invocations."""

    kind = "base"

    def __init__(self, runner_id: str | None = None):
        self.runner_id = runner_id or f"{self.kind}-{uuid.uuid4().hex[:8]}"
        STATE_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def state_file(self) -> Path:
        return STATE_DIR / f"{self.runner_id}.json"

    def _read(self) -> dict:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text())
        return {}

    def _write(self, **kw) -> RunnerStatus:
        d = self._read()
        d.update(kw)
        d.setdefault("runner_id", self.runner_id)
        d.setdefault("provider", self.kind)
        d.setdefault("created", _now())
        d["updated"] = _now()
        self.state_file.write_text(json.dumps(d, indent=2))
        return RunnerStatus(**{k: v for k, v in d.items()
                               if k in RunnerStatus.__dataclass_fields__})

    def status(self) -> RunnerStatus:
        d = self._read()
        if not d:
            return RunnerStatus(self.runner_id, self.kind, "absent",
                                message="no such runner — create it first")
        return RunnerStatus(**{k: v for k, v in d.items()
                               if k in RunnerStatus.__dataclass_fields__})

    @staticmethod
    def list_runners() -> list[dict]:
        """Runner state files only.

        Job specs live in the same directory and are also JSON, so filter on the shape of
        the record rather than trusting the glob — a spec has no runner_id and would
        otherwise be listed as a nameless runner.
        """
        if not STATE_DIR.exists():
            return []
        out = []
        for p in sorted(STATE_DIR.glob("*.json")):
            if p.name.endswith("_job.json"):
                continue
            try:
                rec = json.loads(p.read_text())
            except Exception:
                continue
            if isinstance(rec, dict) and rec.get("runner_id"):
                out.append(rec)
        return out


# --------------------------------------------------------------------------------------
# Local
# --------------------------------------------------------------------------------------

class LocalProvider(BaseProvider):
    """docker compose on this machine. 'Booting' is verifying the image and mounts."""

    kind = "local"

    def __init__(self, runner_id: str | None = None, *, service: str = "pipeline"):
        super().__init__(runner_id)
        self.service = service

    def create(self, **kw) -> RunnerStatus:
        # Merge, never replace: re-creating a runner (to pick up a code change) must not
        # silently discard the storage configuration `mount` wrote. Losing it downgrades
        # an S3 run to a local one that then finds no data.
        self._write(state="creating", message=f"checking docker + {self.service} image",
                    detail={**self._read().get("detail", {}), "service": self.service})
        try:
            v = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                               capture_output=True, text=True, timeout=20)
            if v.returncode != 0:
                return self._write(state="failed", message="docker daemon not responding")
            img = subprocess.run(
                ["docker", "compose", "build", self.service],
                cwd=REPO, capture_output=True, text=True, timeout=1800)
            if img.returncode != 0:
                return self._write(state="failed",
                                   message=(img.stderr or "")[-300:])
        except Exception as exc:
            return self._write(state="failed", message=f"{type(exc).__name__}: {exc}")
        return self._write(state="ready", message="image built, docker reachable",
                           detail={"service": self.service,
                                   "docker": v.stdout.strip()})

    def mount(self, spec: dict) -> dict:
        """Attach storage, and REMEMBER which kind — submit behaves differently for each.

        A local mount is the bind mount from docker-compose.yml: nothing moves. An s3
        mount makes this runner stage data in from the bucket before running and push
        results back after, which is the same data path a pod uses. Running that locally
        is the point: it exercises the storage layer without renting anything.
        """
        kind = spec.get("kind", "local")
        detail = self._read().get("detail", {})
        if kind == "local":
            corpus = (REPO.parent / "corpus_data")
            self._write(detail={**detail, "mount_spec": {"kind": "local"},
                                "mount": {"kind": "local", "path": str(corpus),
                                          "exists": corpus.exists()}})
            return {"ok": corpus.exists(), "kind": "local", "path": str(corpus),
                    "note": "bind mount from docker-compose.yml — nothing moves"}
        if kind == "s3":
            st = _storage()
            chk = st.check() if st.available else {"ok": False, "error": "no credentials"}
            self._write(detail={**detail,
                                "mount_spec": {"kind": "s3",
                                               "root": spec.get("root", "corpus/")},
                                "mount": chk})
            return {"ok": bool(chk.get("ok")), "kind": "s3", **chk,
                    "note": "data will stage in from S3 and results push back — "
                            "same path a pod uses"}
        return {"ok": False, "error": f"unknown mount kind {kind!r}"}

    def _mount_kind(self) -> str:
        return (self._read().get("detail", {})
                .get("mount_spec", {}).get("kind", "local"))

    def push(self, source: str, *, dry_run: bool = False, limit: int | None = None,
             as_source: str | None = None) -> dict:
        """Upload corpus to S3.

        Meaningful even for a local runner: it is how you seed the bucket so an
        s3-mounted local run — or a pod — has something to read.
        """
        raw = REPO.parent / "corpus_data" / "raw" / source
        if not raw.exists():
            return {"ok": False, "error": f"no such source: {raw}"}
        st = _storage()
        if not st.available:
            return {"ok": False, "error": "no S3 credentials (runpods3.key)"}
        dest = as_source or source
        r = st.upload_dir(raw, f"corpus/raw/{dest}", dry_run=dry_run, max_files=limit)
        return {**r, "source": source, "uploaded_as": dest}

    # -- S3 staging: the part a pod also does ------------------------------------------

    def stage_in(self, source: str) -> dict:
        """Pull a source down from S3 into the raw tree the pipeline reads."""
        st = _storage()
        if not st.available:
            return {"ok": False, "error": "no S3 credentials"}
        dst = REPO.parent / "corpus_data" / "raw" / source
        r = st.download_prefix(f"corpus/raw/{source}", dst)
        return {**r, "source": source, "staged_to": str(dst)}

    def stage_out(self, region: str) -> dict:
        """Push results back to S3 so they outlive the runner."""
        st = _storage()
        if not st.available:
            return {"ok": False, "error": "no S3 credentials"}
        out = REPO / "out"
        results = []
        for sub in (f"regions/{region}", "_reports"):
            p = out / sub
            if p.exists():
                results.append(st.upload_dir(p, f"out/{sub}"))
        files = sum(r.get("files", 0) for r in results)
        return {"ok": bool(results), "files": files, "parts": results}

    def submit(self, job: JobSpec) -> RunnerStatus:
        problems = job.validate()
        if problems:
            return self._write(state="failed", job_id=job.job_id,
                               message="; ".join(problems))
        spec_path = STATE_DIR / f"{self.runner_id}_job.json"
        job.save(spec_path)

        kind = self._mount_kind()
        transfer: dict = {"mount": kind}

        # Stage IN — only when mounted to S3. This is the leg a pod also runs, so
        # exercising it locally is what makes the local run a real rehearsal.
        if kind == "s3":
            self._write(state="running", job_id=job.job_id,
                        message="staging corpus in from S3")
            transfer["in"] = [self.stage_in(s["id"]) for s in job.sources]
            failed = [r for r in transfer["in"] if not r.get("ok")]
            if failed:
                return self._write(state="failed",
                                   message=f"stage-in failed: {failed[0].get('error')}",
                                   detail={**self._read().get("detail", {}),
                                           "transfer": transfer})
            got = sum(r.get("files", 0) for r in transfer["in"])
            if got == 0:
                return self._write(
                    state="failed",
                    message="stage-in downloaded 0 files — the bucket has nothing under "
                            "corpus/raw/<source>. Run `push` first.",
                    detail={**self._read().get("detail", {}), "transfer": transfer})
            print(f"    staged in {got} file(s) from S3", flush=True)

        self._write(state="running", job_id=job.job_id,
                    progress={"stage": None, "completed": [], "total": len(job.stages)},
                    message=f"executing {len(job.stages)} stages")

        cmd = ["docker", "compose", "run", "--rm", "--entrypoint", "python",
               self.service, "-m", "runners.execute_job",
               "--spec", f"/out/_reports/runners/{spec_path.name}"]
        t0 = time.time()
        out = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=14400)
        tail = "\n".join((out.stdout or "").strip().splitlines()[-25:])
        if out.returncode != 0:
            return self._write(state="failed",
                               message=(out.stderr or tail or "")[-400:],
                               detail={**self._read().get("detail", {}), "tail": tail,
                                       "transfer": transfer})

        # Stage OUT — results must outlive the runner.
        if kind == "s3":
            transfer["out"] = self.stage_out(job.region)
            print(f"    staged out {transfer['out'].get('files', 0)} result file(s) "
                  f"to S3", flush=True)

        return self._write(state="done",
                           progress={"completed": job.stages, "total": len(job.stages)},
                           message=f"finished in {time.time()-t0:.0f}s",
                           detail={**self._read().get("detail", {}), "tail": tail,
                                   "transfer": transfer})

    def poll(self) -> RunnerStatus:
        # Local submit is synchronous, so state is already final.
        return self.status()

    def fetch(self, dest: Path | None = None) -> dict:
        """Bind-mounted results are already here; S3-mounted results come back down."""
        if self._mount_kind() == "s3":
            st = _storage()
            if not st.available:
                return {"ok": False, "error": "no S3 credentials"}
            return st.download_prefix("out/", dest or (REPO / "out"))
        return {"ok": True, "note": "results already in ./out via bind mount",
                "path": str(REPO / "out")}

    def shutdown(self) -> RunnerStatus:
        subprocess.run(["docker", "compose", "down", "--remove-orphans"],
                       cwd=REPO, capture_output=True, text=True, timeout=120)
        return self._write(state="stopped", message="compose services stopped")

    def destroy(self) -> RunnerStatus:
        self.shutdown()
        s = self._write(state="stopped", message="runner record removed")
        self.state_file.unlink(missing_ok=True)
        return s


# --------------------------------------------------------------------------------------
# RunPod
# --------------------------------------------------------------------------------------

class RunPodProvider(BaseProvider):
    """A pod running the SAME image. Data moves through S3 so a dead pod loses nothing."""

    kind = "runpod"

    def __init__(self, runner_id: str | None = None, *, ssh_host: str | None = None,
                 ssh_port: int = 22, ssh_key: str | None = None,
                 image: str = "linguabackend/gpu:0.1.0", workspace: str = "/workspace"):
        super().__init__(runner_id)
        self.ssh_host, self.ssh_port, self.ssh_key = ssh_host, ssh_port, ssh_key
        self.image, self.workspace = image, workspace

    def _ssh(self, script: str, timeout: int = 3600):
        if not self.ssh_host:
            return subprocess.CompletedProcess([], 1, "", "ssh_host not configured")
        cmd = ["ssh", "-p", str(self.ssh_port)]
        if self.ssh_key:
            cmd += ["-i", self.ssh_key]
        cmd += ["-o", "StrictHostKeyChecking=accept-new", self.ssh_host, "bash", "-s"]
        return subprocess.run(cmd, input=script, capture_output=True, text=True,
                              timeout=timeout)

    def create(self, **kw) -> RunnerStatus:
        """Attach to a pod that already exists, and verify the environment is usable.

        A pod is provisioned in the RunPod console or API with its image chosen at creation
        time — the image must already live in a registry, because the API accepts an
        `imageName`, never a Dockerfile. Nothing here can build one: a pod is itself a
        container with no nested Docker daemon.

        So the only architecture check worth making is on the pod, not on this laptop. A
        local `docker image inspect` proves nothing about what the pod pulled.
        """
        self.ssh_host = kw.get("ssh_host", self.ssh_host)
        self._write(state="creating",
                    detail={**self._read().get("detail", {}),
                            "host": self.ssh_host, "image": self.image})
        if not self.ssh_host:
            return self._write(
                state="failed",
                message="no ssh_host — provision a pod (console or API) and pass --host. "
                        "Its image must already be in a registry; RunPod cannot build one.")
        # Verify the pod itself: architecture, GPU, and that our dependencies import.
        # A pod that is reachable but missing torch fails 20 minutes into a run instead.
        probe = self._ssh(
            "uname -m; nvidia-smi -L 2>/dev/null | head -1; "
            "python -c \"import torch,soundfile,librosa;print('deps ok', torch.__version__)\" "
            "2>&1 | tail -1", timeout=120)
        if probe.returncode != 0:
            return self._write(state="failed", message=(probe.stderr or "")[-300:])
        lines = [l for l in (probe.stdout or "").strip().splitlines() if l]
        arch = lines[0] if lines else "?"
        deps_ok = any("deps ok" in l for l in lines)
        if arch not in ("x86_64", "amd64"):
            return self._write(state="failed",
                               message=f"pod reports arch {arch!r}, expected x86_64")
        return self._write(
            state="ready" if deps_ok else "failed",
            message=("pod reachable, deps present" if deps_ok else
                     "pod reachable but dependencies are MISSING — its image does not "
                     "contain our environment. Fix the pod's image, or install from "
                     "requirements.txt before submitting."),
            detail={"host": self.ssh_host, "image": self.image, "arch": arch,
                    "probe": lines})

    def mount(self, spec: dict) -> dict:
        """S3 is the only sane mount for a disposable pod."""
        if spec.get("kind") != "s3":
            return {"ok": False,
                    "error": "runpod requires mount.kind=s3 — a pod has no access to "
                             "your laptop's filesystem"}
        import sys
        sys.path.insert(0, str(REPO))
        from .storage import Storage
        st = Storage()
        chk = st.check() if st.available else {"ok": False, "error": "no credentials"}
        self._write(detail={**self._read().get("detail", {}), "mount": chk})
        return {"ok": bool(chk.get("ok")), "kind": "s3", **chk}

    def push(self, source: str, *, dry_run: bool = False, limit: int | None = None,
             as_source: str | None = None) -> dict:
        """Upload a source's audio to S3 so the pod can reach it.

        Separate from `submit` on purpose: corpus upload is slow and idempotent, a job is
        fast and re-run often. Folding them together would re-check gigabytes on every
        code fix.
        """
        raw = REPO.parent / "corpus_data" / "raw" / source
        if not raw.exists():
            return {"ok": False, "error": f"no such source: {raw}"}
        st = _storage()
        if not st.available:
            return {"ok": False, "error": "no S3 credentials (runpods3.key)"}
        dest = as_source or source
        r = st.upload_dir(raw, f"corpus/raw/{dest}", dry_run=dry_run, max_files=limit)
        return {**r, "source": source, "uploaded_as": dest}

    def submit(self, job: JobSpec) -> RunnerStatus:
        problems = job.validate()
        if problems:
            return self._write(state="failed", message="; ".join(problems))
        spec_path = STATE_DIR / f"{self.runner_id}_job.json"
        job.save(spec_path)

        import sys
        sys.path.insert(0, str(REPO))
        from .storage import Storage
        st = Storage()
        st.upload_dir(spec_path.parent, "jobs", max_files=None)

        self._write(state="running", job_id=job.job_id,
                    progress={"total": len(job.stages), "completed": []})
        # The pod's own image is the environment — see RunPodExecutor.execute for why there
        # is no docker build/run here. Code runs directly in the pod's container.
        script = f"""
set -e
cd {self.workspace}/code
nohup python -m runners.execute_job --spec {self.workspace}/jobs/{spec_path.name} \\
  > {self.workspace}/job_{job.job_id}.log 2>&1 &
echo "started $!"
"""
        r = self._ssh(script, timeout=600)
        if r.returncode != 0:
            return self._write(state="failed", message=(r.stderr or "")[-300:])
        return self._write(state="running", message="job started on pod (detached)")

    def poll(self) -> RunnerStatus:
        d = self._read()
        job_id = d.get("job_id")
        if not job_id:
            return self.status()
        r = self._ssh(f"tail -5 {self.workspace}/job_{job_id}.log 2>/dev/null; "
                      f"pgrep -f execute_job >/dev/null && echo RUNNING || echo IDLE",
                      timeout=90)
        tail = (r.stdout or "").strip()
        state = "running" if tail.endswith("RUNNING") else "done"
        return self._write(state=state, message=tail[-300:])

    def fetch(self, dest: Path | None = None) -> dict:
        import sys
        sys.path.insert(0, str(REPO))
        from .storage import Storage
        st = Storage()
        if not st.available:
            return {"ok": False, "error": "no S3 credentials"}
        return st.download_prefix("out/", dest or (REPO / "out"))

    def shutdown(self) -> RunnerStatus:
        self._ssh("pkill -f execute_job || true", timeout=60)
        return self._write(state="stopped", message="job processes signalled")

    def destroy(self) -> RunnerStatus:
        self.shutdown()
        s = self._write(state="stopped",
                        message="runner record removed — TERMINATE THE POD in the RunPod "
                                "console or you keep paying for it")
        self.state_file.unlink(missing_ok=True)
        return s


PROVIDERS = {"local": LocalProvider, "runpod": RunPodProvider}


def get_provider(kind: str, runner_id: str | None = None, **kw) -> Provider:
    if kind not in PROVIDERS:
        raise ValueError(f"unknown provider {kind!r}; have {sorted(PROVIDERS)}")
    return PROVIDERS[kind](runner_id, **kw)
