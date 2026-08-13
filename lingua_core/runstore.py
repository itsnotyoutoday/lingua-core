"""Immutable run artifacts. A finished run can never be overwritten by a later one.

## Why this exists

A pod restarted after a successful run, re-executed the same job, and destroyed what the
first run produced: a 132 KB speaker-assignment map became 77 bytes, and an embedding
archive of 2,483 vectors became an empty file. The checkpoint survived claiming all 2,507
files were complete, so a resume would have skipped everything and silently produced
nothing.

The root cause was writing every artifact to a fixed path. `speakers_<source>.json` means
the second run's output lands exactly where the first run's lives. Nothing about that is
recoverable after the fact.

## The rule

Artifacts are written under an immutable, timestamped run directory:

    runs/<region>/<run_id>/          run_id = <utc-timestamp>_<job_id>_<short-hash>
        manifest.json                what ran, from what, producing what
        profile.json
        speakers_<source>.json
        embeddings_<source>.npz
        measurements_<source>.jsonl  ← per-utterance rows, so re-analysis never re-measures

and only then is a POINTER updated:

    regions/<region>/latest.json     -> {"run_id": …}

Reading "the current profile" is two cheap lookups. Rolling back is editing a pointer. A
second run cannot corrupt a first, because it never writes to the same place.

## Why the per-utterance rows matter

The first neutro profile saved only fitted distributions. That made it impossible to ask
whether one 432-clip cluster dominated the pole, or to re-fit per-speaker, without
re-measuring 2,507 files. Measurements are the expensive part and the reusable part; they
are now persisted as JSONL, one row per utterance.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def make_run_id(job_id: str, spec: dict | None = None) -> str:
    """Timestamped and content-hashed, so two runs of the same job never collide."""
    h = hashlib.sha256(
        json.dumps(spec or {}, sort_keys=True, default=str).encode()).hexdigest()[:8]
    return f"{_now_stamp()}_{job_id}_{h}"


@dataclass
class RunManifest:
    run_id: str
    region: str
    job_id: str
    started: str
    spec: dict = field(default_factory=dict)
    finished: str | None = None
    status: str = "running"
    stages: list = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class RunStore:
    """One immutable directory per run, plus a mutable pointer to the current one."""

    def __init__(self, region: str, run_id: str):
        self.region = region
        self.run_id = run_id

    # -- paths --------------------------------------------------------------------------

    @property
    def dir(self) -> Path:
        d = config.OUT_ROOT / "runs" / self.region / self.run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def path(self, name: str) -> Path:
        return self.dir / name

    @staticmethod
    def pointer_path(region: str) -> Path:
        d = config.OUT_ROOT / "regions" / region
        d.mkdir(parents=True, exist_ok=True)
        return d / "latest.json"

    # -- writing ------------------------------------------------------------------------

    def write_json(self, name: str, doc: Any) -> Path:
        p = self.path(name if name.endswith(".json") else f"{name}.json")
        if p.exists():
            # Inside a single run a name is written once. Hitting this means two stages
            # chose the same filename, which would repeat the overwrite bug at smaller
            # scale.
            raise FileExistsError(
                f"{p.name} already exists in run {self.run_id} — artifact names must be "
                f"unique within a run")
        p.write_text(json.dumps(doc, indent=2, ensure_ascii=False))
        return p

    def write_rows(self, name: str, rows: Iterable[dict]) -> Path:
        """JSONL for per-utterance data — streamable, appendable, and diffable."""
        p = self.path(name if name.endswith(".jsonl") else f"{name}.jsonl")
        n = 0
        with p.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
                n += 1
        return p

    def write_bytes(self, name: str, data: bytes) -> Path:
        p = self.path(name)
        p.write_bytes(data)
        return p

    # -- manifest + pointer -------------------------------------------------------------

    def start(self, job_id: str, spec: dict) -> RunManifest:
        m = RunManifest(run_id=self.run_id, region=self.region, job_id=job_id,
                        started=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        spec=spec)
        self.path("manifest.json").write_text(json.dumps(m.as_dict(), indent=2))
        return m

    def finish(self, manifest: RunManifest, *, status: str, stages: list,
               counts: dict | None = None) -> Path:
        manifest.finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
        manifest.status = status
        manifest.stages = stages
        manifest.counts = counts or {}
        manifest.artifacts = {p.name: p.stat().st_size for p in sorted(self.dir.iterdir())
                              if p.is_file()}
        p = self.path("manifest.json")
        p.write_text(json.dumps(manifest.as_dict(), indent=2))
        # The pointer moves ONLY on success. A failed run stays on disk for inspection but
        # never becomes "current", so a broken rerun cannot displace a good result.
        if status == "ok":
            self.promote()
        return p

    def promote(self) -> Path:
        p = self.pointer_path(self.region)
        prev = json.loads(p.read_text()) if p.exists() else {}
        doc = {"region": self.region, "run_id": self.run_id,
               "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "path": f"runs/{self.region}/{self.run_id}",
               "previous_run_id": prev.get("run_id")}
        p.write_text(json.dumps(doc, indent=2))
        return p

    # -- reading ------------------------------------------------------------------------

    @classmethod
    def latest(cls, region: str) -> "RunStore | None":
        p = cls.pointer_path(region)
        if not p.exists():
            return None
        return cls(region, json.loads(p.read_text())["run_id"])

    @classmethod
    def list_runs(cls, region: str) -> list[dict]:
        base = config.OUT_ROOT / "runs" / region
        if not base.exists():
            return []
        out = []
        for d in sorted(base.iterdir(), reverse=True):
            mp = d / "manifest.json"
            if mp.exists():
                try:
                    out.append(json.loads(mp.read_text()))
                except Exception:
                    continue
        return out
