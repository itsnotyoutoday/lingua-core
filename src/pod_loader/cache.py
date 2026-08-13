"""An optional local cache in front of object storage — the layer that makes the design portable.

## Why optional is the whole point

A RunPod network volume persists across pods, so a source pulled once is free thereafter.
That is genuinely valuable, and it is also RunPod-specific: no other provider offers it, and
attaching one PINS compute to its datacenter — which is the failure that blocked every job
today when US-NC-1 filled up.

If the cache were assumed, the design would be married to RunPod. If it were absent, every
job would re-fetch. So it is a configurable layer with three settings, and the code above it
cannot tell which is in force:

    persistent   root on a network volume    survives the pod   fastest, pinned
    ephemeral    root on container disk      dies with the pod  portable, re-pull per pod
    disabled     no root at all              nothing cached     stream straight through

Same calls, different economics. Moving providers changes a config value, not a line of
logic.

## Why a cache and not just "read from S3"

Two stages read the same audio: `normalize` writes it, `align` and `measure` read it back,
and MFA is a subprocess that needs real files on disk regardless. Something has to
materialise those bytes locally. The only question is whether they survive the pod, and
that is exactly what this makes configurable.

## Eviction

Least-recently-used, triggered by a byte ceiling. Safe by construction: everything here is
a copy of something in object storage, so eviction costs a re-fetch and never data. The
`.lingua-cache.json` index records access times; if it is lost, eviction falls back to
filesystem mtime rather than refusing to run — an index is a convenience, never the truth.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

INDEX = ".lingua-cache.json"


@dataclass
class CacheConfig:
    """How (and whether) to cache locally.

    Resolved from environment so a pod is told at launch, the same way it is told its mount
    kind — the job does not decide, the launcher does.
    """
    enabled: bool = True
    root: Path | None = None
    max_bytes: int = 0                 # 0 = no ceiling; evict only when asked
    persistent: bool = False           # informational: does it survive the pod?

    @classmethod
    def from_env(cls) -> "CacheConfig":
        if os.environ.get("PODH_CACHE_DISABLED", "").lower() in ("1", "true", "yes"):
            return cls(enabled=False)
        root = os.environ.get("PODH_CACHE_DIR")
        if not root:
            # Default to the workspace, which IS the network volume when one is attached
            # and ordinary container disk when one is not. The caller cannot tell, and
            # should not need to.
            from . import paths
            root = str(paths.workspace())
        max_gb = float(os.environ.get("PODH_CACHE_MAX_GB", "0") or 0)
        return cls(enabled=True, root=Path(root), max_bytes=int(max_gb * 1e9),
                   persistent=os.environ.get("PODH_CACHE_PERSISTENT", "") == "1")

    def describe(self) -> dict:
        return {"enabled": self.enabled, "root": str(self.root) if self.root else None,
                "max_gb": round(self.max_bytes / 1e9, 1) if self.max_bytes else None,
                "persistent": self.persistent}


class Cache:
    """Local materialisation with an LRU index. Every entry is a copy; nothing is authoritative."""

    def __init__(self, cfg: CacheConfig | None = None):
        self.cfg = cfg or CacheConfig.from_env()
        self._index: dict[str, dict] = {}
        if self.enabled:
            self.cfg.root.mkdir(parents=True, exist_ok=True)
            self._load_index()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.root)

    # -- index ---------------------------------------------------------------------------

    @property
    def _index_path(self) -> Path:
        return self.cfg.root / INDEX

    def _load_index(self) -> None:
        try:
            self._index = json.loads(self._index_path.read_text())
        except Exception:
            self._index = {}          # a lost index costs accuracy, never correctness

    def _save_index(self) -> None:
        try:
            tmp = self._index_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._index))
            tmp.replace(self._index_path)
        except Exception:
            pass                      # never fail a job over cache bookkeeping

    def touch(self, key: str, size: int = 0) -> None:
        e = self._index.setdefault(key, {"bytes": size, "hits": 0})
        e["at"] = time.time()
        e["hits"] = e.get("hits", 0) + 1
        if size:
            e["bytes"] = size
        self._save_index()

    # -- lookup --------------------------------------------------------------------------

    def path_for(self, key: str) -> Path | None:
        """Where this key WOULD live locally, or None when caching is off."""
        return (self.cfg.root / key) if self.enabled else None

    def hit(self, key: str) -> Path | None:
        """The local path if present, else None. Presence is asked of the filesystem, not
        of the index — an index that claims a file exists after a volume was wiped is the
        `.DONE` marker bug in a new costume."""
        p = self.path_for(key)
        if p and p.exists():
            self.touch(key)
            return p
        return None

    # -- eviction ------------------------------------------------------------------------

    def size(self) -> int:
        if not self.enabled:
            return 0
        return sum(f.stat().st_size for f in self.cfg.root.rglob("*")
                   if f.is_file() and f.name != INDEX)

    def evict_to(self, target_bytes: int | None = None) -> dict:
        """Drop least-recently-used entries until under the ceiling.

        Entries are top-level directories under the cache root, not individual files: a
        half-evicted source is worse than an absent one, because it looks complete.
        """
        if not self.enabled:
            return {"evicted": [], "freed": 0, "reason": "cache disabled"}
        ceiling = target_bytes if target_bytes is not None else self.cfg.max_bytes
        if not ceiling:
            return {"evicted": [], "freed": 0, "reason": "no ceiling configured"}

        entries = []
        for child in self.cfg.root.iterdir():
            if child.name == INDEX:
                continue
            b = sum(f.stat().st_size for f in child.rglob("*") if f.is_file()) \
                if child.is_dir() else child.stat().st_size
            rel = child.name
            at = self._index.get(rel, {}).get("at")
            if at is None:
                try:
                    at = child.stat().st_mtime      # fall back to the filesystem
                except OSError:
                    at = 0
            entries.append((at, rel, child, b))

        total = sum(e[3] for e in entries)
        evicted, freed = [], 0
        for at, rel, path, b in sorted(entries):        # oldest first
            if total - freed <= ceiling:
                break
            shutil.rmtree(path) if path.is_dir() else path.unlink()
            self._index.pop(rel, None)
            evicted.append(rel)
            freed += b
        self._save_index()
        return {"evicted": evicted, "freed": freed,
                "before": total, "after": total - freed, "ceiling": ceiling}

    def stats(self) -> dict:
        return {**self.cfg.describe(), "entries": len(self._index),
                "bytes": self.size() if self.enabled else 0}
