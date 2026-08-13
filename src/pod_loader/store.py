"""Typed storage access — the kind of a thing decides where it lives and what may be done to it.

## Why a kind rather than a path

`paths.py` gives every location one definition, which stops paths being invented. It does
not stop a caller doing the wrong THING to a location: overwriting raw corpus audio,
deleting a release an installed app still pins to, or rewriting a finished run.

Those are not path mistakes; they are permission mistakes, and a string cannot carry
permissions. So each artifact kind declares its own rules, and every write, list and read
goes through them:

    Kind.CORPUS_RAW    write-once, never delete   the ONLY thing that cannot be recreated
    Kind.DERIVED       overwrite, delete freely   expensive but reproducible
    Kind.RUN_OUTPUT    write-once per run         a second run must not corrupt a first
    Kind.PROFILE       versioned + pointer        promotion is explicit
    Kind.AUTHORED      overwrite, never auto-GC   a human made it; compute cannot remake it
    Kind.GENERATED     content-keyed, write-once  same request, same object, served not remade
    Kind.RELEASE       write-once, forever        app builds pin to versions
    Kind.CACHE         anything, delete anytime   trivially recreatable
    Kind.CODE          write-once per sha         except dev/, mutable on purpose
    Kind.TMP           anything, swept            the loud escape hatch

## The rule that earns its keep

`CORPUS_RAW` refusing overwrite is not theoretical. `runstore.py` exists because a restarted
pod re-ran a job and *"a 132 KB speaker-assignment map became 77 bytes"* — every artifact
was written to a fixed path, so the second run destroyed the first. Write-once, enforced
here, makes that class of loss impossible rather than merely unlikely.

## Reading and listing are typed too

`ls(Kind.DERIVED, stage="normalize")` cannot accidentally enumerate 87,000 corpus objects,
and `read(Kind.RELEASE, ...)` cannot silently fall back to something mutable. The kind names
the intent; the store enforces it.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from . import paths


@dataclass(frozen=True)
class Rules:
    """What may be done to a kind of artifact, and why."""
    overwrite: bool          # may an existing object be replaced?
    deletable: bool          # may it be removed by ordinary cleanup?
    gc: bool                 # may automatic retention remove it?
    reason: str              # stated so a refusal explains itself


class Kind(Enum):
    """An artifact kind. `.rules` says what is permitted; `.locate()` says where it goes."""

    CORPUS_RAW = "corpus_raw"
    CORPUS_RECIPE = "corpus_recipe"
    CORPUS_MANIFEST = "corpus_manifest"
    DERIVED = "derived"
    AUTHORED = "authored"
    GENERATED = "generated"
    PROFILE = "profile"
    RUN_META = "run_meta"
    RUN_OUTPUT = "run_output"
    RELEASE = "release"
    CACHE = "cache"
    CODE = "code"
    TMP = "tmp"

    @property
    def rules(self) -> Rules:
        return _RULES[self]

    def locate(self, **kw) -> str:
        """The key for this kind. Keyword arguments are checked, so a missing `source_id`
        fails here rather than producing a plausible-looking wrong key."""
        return _LOCATORS[self](**kw)


_RULES: dict[Kind, Rules] = {
    Kind.CORPUS_RAW: Rules(
        overwrite=False, deletable=False, gc=False,
        reason="raw corpus audio is the only thing in the system that cannot be recomputed "
               "or re-authored; overwriting or deleting it is unrecoverable"),
    Kind.CORPUS_RECIPE: Rules(
        overwrite=True, deletable=True, gc=False,
        reason="a corpus definition is an editable input"),
    Kind.CORPUS_MANIFEST: Rules(
        overwrite=True, deletable=False, gc=False,
        reason="the source manifest is edited as sources are added, but never dropped"),
    Kind.DERIVED: Rules(
        overwrite=True, deletable=True, gc=True,
        reason="expensive but reproducible from corpus/raw; safe to drop and recompute"),
    Kind.AUTHORED: Rules(
        overwrite=True, deletable=False, gc=False,
        reason="a person recorded or wrote it; no amount of compute recreates it"),
    Kind.GENERATED: Rules(
        overwrite=False, deletable=True, gc=True,
        reason="content-keyed, so the same key IS the same content — rewriting it means "
               "the key was computed wrongly"),
    Kind.PROFILE: Rules(
        overwrite=False, deletable=True, gc=False,
        reason="profiles are versioned per run and selected by a pointer; a second run "
               "must never overwrite a first"),
    Kind.RUN_META: Rules(
        overwrite=True, deletable=True, gc=True,
        reason="status and events are appended to while a run is live"),
    Kind.RUN_OUTPUT: Rules(
        overwrite=False, deletable=True, gc=True,
        reason="what a run produced is that run's record; a rerun writes a new run"),
    Kind.RELEASE: Rules(
        overwrite=False, deletable=False, gc=False,
        reason="installed app builds pin to a version; fix a bad asset by publishing a new "
               "version, never by overwriting an old one"),
    Kind.CACHE: Rules(
        overwrite=True, deletable=True, gc=True,
        reason="trivially recreatable"),
    Kind.CODE: Rules(
        overwrite=False, deletable=True, gc=True,
        reason="a <sha> directory is immutable — a push to a mutable path can change code "
               "under a running job, and lazy imports would mix versions mid-run"),
    Kind.TMP: Rules(
        overwrite=True, deletable=True, gc=True,
        reason="explicitly temporary; nothing may depend on it surviving"),
}

_LOCATORS: dict[Kind, Callable[..., str]] = {
    Kind.CORPUS_RAW: lambda source_id, name="": f"{paths.corpus_raw(source_id)}/{name}".rstrip("/"),
    Kind.CORPUS_RECIPE: lambda name: paths.corpus_recipes(name),
    Kind.CORPUS_MANIFEST: lambda: paths.corpus_manifest(),
    Kind.DERIVED: lambda stage, source_id, name="": f"{paths.derived(stage, source_id)}/{name}".rstrip("/"),
    Kind.AUTHORED: lambda kind, name="": paths.authored(kind, name),
    Kind.GENERATED: lambda kind, content_key, name="": f"{paths.generated(kind, content_key)}/{name}".rstrip("/"),
    Kind.PROFILE: lambda region, name="": f"{paths.profiles(region)}/{name}".rstrip("/"),
    Kind.RUN_META: lambda job_id, name: f"{paths.run(job_id)}/{name}",
    Kind.RUN_OUTPUT: lambda job_id, name="": paths.run_out(job_id, name),
    Kind.RELEASE: lambda version, name="": paths.release(version, name),
    Kind.CACHE: lambda kind="", name="": f"{paths.cache(kind)}/{name}".rstrip("/"),
    Kind.CODE: lambda repo, rev, name="": f"{paths.code(repo, rev)}/{name}".rstrip("/"),
    Kind.TMP: lambda name, reason="": paths.tmp(name, reason=reason),
}


class PermissionError_(PermissionError):
    """A write or delete the kind's rules forbid. Carries the reason, because 'denied' with
    no explanation just gets worked around."""


class Store:
    """Typed access to object storage. The single door for reads, writes and listings.

    Wraps `Storage`, which handles credentials and the S3 client. This layer adds the part
    a client cannot: knowing what kind of thing is being touched, and what that permits.
    """

    def __init__(self, storage: Any = None, *, profile: str | None = None,
                 cache: Any = None):
        if storage is None:
            from .objectstore import get_storage
            storage = get_storage(profile)
        self.storage = storage
        # The cache is a LAYER, not an assumption. Persistent on a RunPod volume, ephemeral
        # on container disk, or absent entirely — the calls below cannot tell which, so
        # moving providers is a config change rather than a rewrite.
        if cache is None:
            from .cache import Cache
            cache = Cache()
        self.cache = cache

    # -- helpers -------------------------------------------------------------------------

    def _cfg(self):
        return self.storage.require()

    def exists(self, key: str) -> bool:
        try:
            self.storage.client.head_object(Bucket=self._cfg().bucket, Key=key)
            return True
        except Exception:
            return False

    # -- write ---------------------------------------------------------------------------

    def write(self, kind: Kind, body: bytes, *, force: bool = False,
              where: str = "", **loc) -> dict:
        """Write one object of a given kind.

        `force` overrides a write-once rule, and is deliberately explicit: overwriting
        raw corpus audio or a shipped release should be a decision someone typed, not a
        default someone inherited.
        """
        key = paths.validate(kind.locate(**loc), where=where or kind.name)
        r = kind.rules
        if not r.overwrite and not force and self.exists(key):
            raise PermissionError_(
                f"{kind.name} is write-once and {key} already exists.\n"
                f"  Why: {r.reason}\n"
                f"  If replacing it is genuinely correct, pass force=True — but consider "
                f"whether a NEW version (a new run, sha or release) is what you want.")
        return self.storage.put(key, body, where=where or kind.name)

    # -- read ----------------------------------------------------------------------------

    def read(self, kind: Kind, *, missing_ok: bool = False, **loc) -> bytes | None:
        key = paths.validate(kind.locate(**loc), where=kind.name)
        try:
            return self.storage.client.get_object(
                Bucket=self._cfg().bucket, Key=key)["Body"].read()
        except Exception:
            if missing_ok:
                return None
            raise

    # -- list ----------------------------------------------------------------------------

    def ls(self, kind: Kind, *, limit: int = 1000, **loc) -> list[dict]:
        """List within one kind.

        Typed so a listing cannot accidentally enumerate the whole bucket — `ls(CORPUS_RAW)`
        with no source is 87,000 objects, and RunPod's ListObjects is documented as
        degrading on very large volumes.
        """
        prefix = paths.validate(kind.locate(**loc), where=kind.name).rstrip("/") + "/"
        out = []
        pag = self.storage.client.get_paginator("list_objects_v2")
        for page in pag.paginate(Bucket=self._cfg().bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                out.append({"key": o["Key"], "bytes": o["Size"],
                            "modified": str(o.get("LastModified", ""))})
                if len(out) >= limit:
                    return out
        return out

    # -- delete --------------------------------------------------------------------------

    def delete(self, kind: Kind, *, force: bool = False, where: str = "", **loc) -> dict:
        key = paths.validate(kind.locate(**loc), where=where or kind.name)
        r = kind.rules
        if not r.deletable and not force:
            raise PermissionError_(
                f"{kind.name} is not deletable.\n  Why: {r.reason}\n"
                f"  Pass force=True only if you are certain this is not the last copy.")
        return self.storage.client.delete_object(Bucket=self._cfg().bucket, Key=key)

    def gc_candidates(self, kind: Kind, **loc) -> list[dict]:
        """What automatic retention may remove for this kind — empty when gc is forbidden,
        so a sweep can never reach corpus, authored assets or releases."""
        if not kind.rules.gc:
            return []
        return self.ls(kind, **loc)

    # -- materialise ---------------------------------------------------------------------

    def fetch(self, kind: Kind, *, dest: Any = None, **loc):
        """Bring a whole artifact local and return the path to it.

            store.fetch(Kind.CODE, repo="lingua-trainer", rev="a1b2c3d")  -> Path
            store.fetch(Kind.CORPUS_RAW, source_id="openslr_82")          -> Path

        The important property: **on a mounted volume this is a no-op.** The bytes are
        already at `/workspace/<key>`, so the call returns that path without transferring
        anything. On object storage it downloads to scratch and returns that instead.

        Same call, both worlds — which is what makes `mount.kind` invisible to a stage.
        A stage asks for its inputs by kind and identity; whether they arrived by mount or
        by download is the runner's problem, not the pipeline's.
        """
        from pathlib import Path

        key = paths.validate(kind.locate(**loc), where=f"{kind.name}.fetch")

        hit = self.cache.hit(key)
        if hit is not None:
            return hit                      # already local — no transfer, no cost

        if not self.cache.enabled and dest is None:
            raise RuntimeError(
                f"caching is disabled and no dest given for {key}. Either enable the cache "
                f"(PODH_CACHE_DIR) or pass dest=, or use stream() if the consumer can "
                f"take a file object rather than a path.")

        dest = Path(dest) if dest else self.cache.path_for(key)
        dest.mkdir(parents=True, exist_ok=True)
        self.storage.download_prefix(key, dest)
        if self.cache.enabled:
            self.cache.touch(key, sum(f.stat().st_size for f in dest.rglob("*")
                                      if f.is_file()))
        return dest

    def stream(self, kind: Kind, **loc):
        """A file-like object, no disk involved.

        For consumers that accept a stream — librosa, soundfile, pypdf. NOT for MFA: Kaldi
        is a subprocess and needs a real path, which is precisely why the cache exists at
        all rather than everything being streamed.
        """
        key = paths.validate(kind.locate(**loc), where=f"{kind.name}.stream")
        return self.storage.client.get_object(
            Bucket=self._cfg().bucket, Key=key)["Body"]

    def push(self, kind: Kind, local_dir: Any, *, force: bool = False,
             where: str = "", **loc) -> dict:
        """Upload a directory as an artifact of this kind.

        Rules are applied to the PREFIX, so a write-once kind refuses when anything already
        exists there — a half-overwritten artifact directory is worse than a refused write,
        because it looks complete.
        """
        from pathlib import Path

        prefix = paths.validate(kind.locate(**loc), where=where or f"{kind.name}.push")
        r = kind.rules
        if not r.overwrite and not force and self.ls(kind, limit=1, **loc):
            raise PermissionError_(
                f"{kind.name} is write-once and {prefix}/ is not empty.\n"
                f"  Why: {r.reason}\n"
                f"  A partial overwrite of a directory is worse than a refusal: it looks "
                f"complete. Write a new version, or pass force=True deliberately.")
        return self.storage.upload_dir(Path(local_dir), prefix)
