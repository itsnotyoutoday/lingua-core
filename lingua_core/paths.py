"""Every storage location, defined once.

## Why this file exists

Before it, path literals lived in a dozen places — `f"corpus/raw/{source}"` in provider.py,
`f"status/{job_id}/status.json"` in events.py, `f"code/{repo}/{rev}"` in sync.py, `"out/"`
in executor.py — and they had already drifted into three incompatible layouts:

    runstore.py   runs/<region>/<run_id>/  +  regions/<region>/latest.json
    store.py      out/index.json, out/items/, out/profiles/<region>.json
    events.py     logs/<pod_id>/jobs/<job_id>/  +  status/<job_id>/

Three schemes for one bucket is why S3 looked cluttered. A shared vocabulary is the fix, and
it only works if there is exactly one definition of it.

## The organising question

Top level answers one thing: **did we GET it, or did we MAKE it?**

    corpus/     external input. Cannot be recreated by us. Never delete.
    assets/     everything we produce or author.
    runs/       what happened. Cannot be recreated. Prunable by age.
    releases/   shippable, versioned, immutable — app builds pin to these.
    cache/      trivially recreatable. Delete anytime.
    code/       scripts, published by CI, addressed by commit.

Anything new earns a root only if it needs a DIFFERENT POLICY — different retention,
access, or deletion rules. Different contents alone is not enough; that is what
subdirectories are for.

## One layout, two views

A RunPod network volume is the same bytes seen two ways: an S3 key from outside, a POSIX
path from inside the pod. So every location here has both, derived from the same parts —
`key()` for object storage, `path()` for the mount. They cannot drift because neither is
written by hand.

## What is NOT here

`contributed/` — opt-in user recordings. It belongs in its own BUCKET, not a prefix:
personal data carries consent records, retention limits and deletion requests, and those
are bucket-level concerns. "Delete everything for user X" must be a prefix delete in a
bucket containing nothing else, never a search across 87,000 corpus objects.

Client-facing `generated/` and `releases/` also live in R2 rather than the volume, because
RunPod's S3 API supports no presigned URLs. See STRUCTURE.md.
"""
from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------------------
# Roots
# --------------------------------------------------------------------------------------

CORPUS = "corpus"
ASSETS = "assets"
RUNS = "runs"
RELEASES = "releases"
CACHE = "cache"
CODE = "code"

#: Legal, and deliberately ugly. Its existence is what stops "I need somewhere to put this"
#: from becoming a new top-level prefix nobody agreed to. Sweepable at any time.
TMP = "_tmp"

ROOTS = (CORPUS, ASSETS, RUNS, RELEASES, CACHE, CODE, TMP)

#: Where the volume is mounted inside a container. The S3 key `corpus/raw/x` is the file
#: `/workspace/corpus/raw/x` — same bytes, two views.
def workspace() -> Path:
    return Path(os.environ.get("LINGUA_WORKSPACE", "/workspace"))


def path(key: str) -> Path:
    """S3 key -> local path on the mount."""
    return workspace() / key


def key(p: str | Path) -> str:
    """Local path on the mount -> S3 key. Raises if the path is outside the workspace,
    because a silently-wrong key writes data somewhere nobody will look for it."""
    p = Path(p).resolve()
    ws = workspace().resolve()
    try:
        return p.relative_to(ws).as_posix()
    except ValueError:
        raise ValueError(f"{p} is not under the workspace {ws}") from None


# --------------------------------------------------------------------------------------
# corpus/ — external input, immutable, never deleted
# --------------------------------------------------------------------------------------

def corpus_raw(source_id: str = "") -> str:
    """As downloaded. Never modified — every derived form is reproducible from here, and
    nothing else is."""
    return f"{CORPUS}/raw/{source_id}".rstrip("/")


def corpus_manifest() -> str:
    """corpus_research.json — which sources exist and under what licence."""
    return f"{CORPUS}/corpus_research.json"


def corpus_recipes(name: str = "") -> str:
    """Corpus definitions (neutro.json, cuba.json). Inputs describing what to build, so
    they belong beside the corpus rather than in code/."""
    return f"{CORPUS}/recipes/{name}".rstrip("/")


# --------------------------------------------------------------------------------------
# assets/ — everything we produce or author
# --------------------------------------------------------------------------------------

def derived(stage: str = "", source_id: str = "") -> str:
    """Pipeline computations: normalized audio, alignments, embeddings, measurements.

    Expensive but reproducible, so safe to delete — and keyed by a derivation sidecar
    (`_derivation.json`) so a stage can tell whether existing output is CURRENT rather
    than merely present. Existence is not currency; that distinction is what stops
    re-normalising 87,000 files for no reason.
    """
    return f"{ASSETS}/derived/{stage}/{source_id}".rstrip("/")


def derivation_meta(stage: str, source_id: str) -> str:
    return f"{derived(stage, source_id)}/_derivation.json"


def authored(kind: str = "", name: str = "") -> str:
    """Human-made and irreplaceable: native reference recordings, lesson content, cue text.

    Not external, not computed — a person made it. No amount of compute recreates it, so it
    is backed up and never garbage-collected.
    """
    return f"{ASSETS}/authored/{kind}/{name}".rstrip("/")


def generated(kind: str = "", content_key: str = "") -> str:
    """Produced on demand for a consumer — synthesised audio, rendered exports.

    CONTENT-KEYED: the key is a hash of the request (text, voice, region, model version), so
    the same request resolves to the same object and is served rather than regenerated. That
    is the whole mechanism for "do not reproduce the same artifact twice".

    Client-facing generation belongs in R2, not the volume — RunPod's S3 API supports no
    presigned URLs.
    """
    return f"{ASSETS}/generated/{kind}/{content_key}".rstrip("/")


def profiles(region: str = "") -> str:
    """The promoted output: the current learned profile for a region.

    A pointer lives beside the versions (`current`), following runstore.py's rule — a second
    run must never overwrite a first, so runs write immutable directories and only the
    pointer moves. Rolling back is editing a pointer.
    """
    return f"{ASSETS}/profiles/{region}".rstrip("/")


# --------------------------------------------------------------------------------------
# runs/ — what happened
# --------------------------------------------------------------------------------------

def run(job_id: str) -> str:
    """Everything one job produced, under one prefix.

    Previously scattered across logs/, out/ and status/, which meant "what did this job do"
    was three lookups and cleanup was impossible to do safely. Job ids are ULIDs and sort
    chronologically, so "prune runs older than X" is a prefix range.
    """
    return f"{RUNS}/{job_id}"


def run_spec(job_id: str) -> str:      return f"{run(job_id)}/spec.json"
def run_status(job_id: str) -> str:    return f"{run(job_id)}/status.json"
def run_events(job_id: str) -> str:    return f"{run(job_id)}/events.jsonl"
def run_log(job_id: str) -> str:       return f"{run(job_id)}/job.log"
def run_console(job_id: str) -> str:   return f"{run(job_id)}/console.log"
def run_out(job_id: str, name: str = "") -> str:
    """This run's artifacts. Distinct from assets/ — `runs/<id>/out/profile.json` is what
    THIS run produced; `assets/profiles/<region>/current` is what we decided to keep.
    Without that split you either never delete a run or you lose things you cared about."""
    return f"{run(job_id)}/out/{name}".rstrip("/")


# --------------------------------------------------------------------------------------
# the rest
# --------------------------------------------------------------------------------------

def code(repo: str = "", rev: str = "") -> str:
    """Scripts only, addressed by commit.

    `<sha>` directories are immutable: a push to a mutable path can change code under a
    RUNNING job, and Python's lazy imports mean stage 4 could load a different version than
    stage 1. `dev` is mutable on purpose, for the edit-run loop.
    """
    return f"{CODE}/{repo}/{rev}".rstrip("/")


def code_latest(repo: str) -> str:     return f"{CODE}/{repo}/latest"
def capabilities(repo: str, rev: str) -> str:
    return f"{code(repo, rev)}/capabilities.json"


def release(version: str = "", name: str = "") -> str:
    """Shippable and immutable — an installed app pins to a version, so a bad asset is
    fixed by publishing a new version, never by overwriting an old one."""
    return f"{RELEASES}/{version}/{name}".rstrip("/")


def cache(kind: str = "") -> str:
    """Trivially recreatable: HF weights, MFA models, joblib. Safe to delete at any moment,
    which is exactly why it must not hold anything expensive."""
    return f"{CACHE}/{kind}".rstrip("/")


# --------------------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------------------

def check(k: str) -> list[str]:
    """Problems with a key. Used by tests and the migration to keep the bucket honest."""
    problems = []
    top = k.split("/")[0]
    if top not in ROOTS:
        problems.append(f"{k!r}: top-level {top!r} is not one of {ROOTS}")
    if k.startswith(f"{CODE}/") and len(k.split("/")) < 3:
        problems.append(f"{k!r}: code/ must be code/<repo>/<rev>/…")
    if "//" in k or k.startswith("/"):
        problems.append(f"{k!r}: malformed key")
    return problems


class StructureError(ValueError):
    """A write to a location the structure does not define.

    Raised at the WRITE, not discovered later in the bucket. The named helpers above cover
    every location we have agreed on; anything else is either a location we forgot (extend
    paths.py and STRUCTURE.md together) or genuinely temporary (use `tmp()`).

    Silently allowing an unknown prefix is how three incompatible layouts grew in one
    bucket. The whole point of this module is that it cannot happen by accident.
    """


def validate(k: str, *, where: str = "") -> str:
    """Return the key, or raise with an actionable message. Call this at every write.

    Enforcement lives at the storage boundary rather than in review comments, because a
    convention that depends on remembering is a convention that erodes.
    """
    problems = check(k)
    if not problems:
        return k
    ctx = f" (from {where})" if where else ""
    raise StructureError(
        f"{'; '.join(problems)}{ctx}\n"
        f"  Known roots: {', '.join(ROOTS)}\n"
        f"  If this is a location we should support, add a helper to lingua_core/paths.py\n"
        f"  AND document it in STRUCTURE.md — they change together, on purpose.\n"
        f"  If it is genuinely one-off, use paths.tmp(name, reason=…), which is sweepable\n"
        f"  and obviously temporary rather than quietly permanent.")


def tmp(name: str, *, reason: str = "") -> str:
    """The deliberate escape hatch, for things that truly do not belong anywhere yet.

    Lands under `_tmp/`, which is a legal root precisely so that "I need somewhere to put
    this" never becomes "I invented a new top-level prefix". Everything here is sweepable
    at any time and nothing may depend on it surviving.

    The `reason` is not decoration: it is what tells the next person whether this was a
    genuine one-off or a location that should have been named properly.
    """
    import warnings
    warnings.warn(
        f"paths.tmp({name!r}) — unstructured write"
        + (f": {reason}" if reason else "")
        + ". Sweepable at any time; if it needs to persist, name it in STRUCTURE.md.",
        stacklevel=2)
    return f"{TMP}/{name}".rstrip("/")
