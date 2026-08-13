"""Publish a workload's source to object storage, so a pod runs THIS code.

## Why code travels as objects and not as a git clone

Cloning from the pod would put a credential on every pod and make GitHub a runtime
dependency of every launch — two failure modes bought for no benefit. The pod holds no
credentials; it reads files that were already placed where it can see them.

## Why the destination is content-addressed

    code/<workload>/<rev>/      immutable — a published commit
    code/<workload>/dev/        mutable — laptop iteration, deliberately

Publishing to a mutable path can change code under a RUNNING job, and Python's lazy
imports would then mix two versions inside one process: a stage imported at minute 1 and
a helper imported at minute 40 need not agree. Immutable revision directories make that
impossible. `dev/` stays mutable because iteration needs it, and jobs record when they
used it so a confusing result can be traced back to a moving target.

I have already been bitten by the mutable-path version of this: `pip install …@main`
served a stale engine from a Docker layer cache while claiming to be current. The fix was
pinning by SHA — the same argument, one layer up.

## Why this replaced the previous version

`sync_code()` walked hardcoded directories — `pipeline`, `runners`, `corpora`, `jobs` —
which were the folders of the pre-split monolith. After the repo split it silently pushed
nothing relevant, and the first pod to need `maintenance` failed with
`ModuleNotFoundError: No module named 'maintenance'`. Generic machinery carrying one
workload's folder names looks harmless right up to the moment there are two workloads.

So this takes a repo path and publishes its `code/` tree, whatever is in it.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

#: Source and small metadata only. No corpora, no caches, and never a key file — the
#: pattern exclusion is belt-and-braces on top of `.gitignore`, because this pushes to a
#: bucket the pod can read and a leaked credential there is a leaked credential.
INCLUDE_SUFFIXES = (".py", ".json", ".yaml", ".yml", ".txt", ".md", ".toml", ".cfg")
EXCLUDE_PARTS = ("__pycache__", ".git", ".venv", "node_modules", ".pytest_cache",
                 ".mypy_cache", ".ruff_cache")
EXCLUDE_SUFFIXES = (".key", ".pem", ".env")


class SyncError(RuntimeError):
    pass


def _rev(repo: Path) -> str:
    """The commit, or `dev` when the tree is dirty or not a repo.

    A dirty tree MUST NOT publish under a commit sha: the sha would name code that is not
    what ran, and the next person to check out that sha would get something else. Being
    honest about `dev` costs nothing and keeps immutable paths actually immutable.
    """
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return "dev"
        sha = out.stdout.strip()
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return "dev" if dirty else sha
    except Exception:
        return "dev"


def _files(code_dir: Path) -> list[Path]:
    out = []
    for p in sorted(code_dir.rglob("*")):
        if not p.is_file():
            continue
        if any(part in EXCLUDE_PARTS for part in p.parts):
            continue
        if p.suffix in EXCLUDE_SUFFIXES or p.name.startswith("."):
            continue
        if p.suffix not in INCLUDE_SUFFIXES:
            continue
        out.append(p)
    return out


def publish(repo: str | Path, *, workload: str | None = None, rev: str | None = None,
            store=None, dry_run: bool = False) -> dict:
    """Push `<repo>/code/**` to `code/<workload>/<rev>/`. Returns the root a job should use.

    The returned `root` is what goes into the spec's `code.root` and reaches the pod as
    `LINGUA_CODE_ROOT`.
    """
    repo = Path(repo).resolve()
    code_dir = repo / "code"
    if not code_dir.is_dir():
        raise SyncError(
            f"{repo} has no code/ directory.\n"
            f"  A workload repo keeps its importable packages under code/, so that "
            f"code/<pkg>/ becomes an import root when the volume is on sys.path.")

    workload = workload or repo.name
    rev = rev or _rev(repo)
    root = f"code/{workload}/{rev}"

    files = _files(code_dir)
    if not files:
        raise SyncError(f"{code_dir} contains no publishable files — refusing to publish "
                        f"an empty code root, which would fail on the pod as a confusing "
                        f"ModuleNotFoundError instead of here as a clear one")

    manifest = {
        "workload": workload, "rev": rev, "root": root,
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mutable": rev == "dev",
        "files": {}, "packages": sorted(
            {p.relative_to(code_dir).parts[0] for p in files
             if len(p.relative_to(code_dir).parts) > 1}),
    }

    total = 0
    for p in files:
        rel = p.relative_to(code_dir).as_posix()
        data = p.read_bytes()
        manifest["files"][rel] = {"bytes": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()[:16]}
        total += len(data)

    result = {"workload": workload, "rev": rev, "root": root,
              "files": len(files), "kilobytes": round(total / 1e3, 1),
              "packages": manifest["packages"], "mutable": rev == "dev",
              "dry_run": dry_run}

    if dry_run:
        return result

    if store is None:
        from .objectstore import get_storage
        store = get_storage()
    cfg = store.require()

    for p in files:
        rel = p.relative_to(code_dir).as_posix()
        store.client.put_object(Bucket=cfg.bucket, Key=f"{root}/{rel}",
                                Body=p.read_bytes())
    store.client.put_object(Bucket=cfg.bucket, Key=f"{root}/.manifest.json",
                            Body=json.dumps(manifest, indent=2).encode())
    # A `latest` pointer so a launcher can resolve without knowing the sha. Deliberately a
    # pointer and not a copy: one small object to update, and nothing can be half-updated.
    store.client.put_object(Bucket=cfg.bucket, Key=f"code/{workload}/latest",
                            Body=rev.encode())
    return result


def resolve(workload: str, rev: str = "latest", store=None) -> str:
    """Turn (workload, rev) into a code root, following `latest` when asked."""
    if rev not in ("latest", ""):
        return f"code/{workload}/{rev}"
    if store is None:
        from .objectstore import get_storage
        store = get_storage()
    cfg = store.require()
    try:
        sha = store.client.get_object(
            Bucket=cfg.bucket, Key=f"code/{workload}/latest")["Body"].read().decode().strip()
    except Exception as e:
        raise SyncError(
            f"no published code for workload {workload!r} ({type(e).__name__}).\n"
            f"  Publish it first: python -m pod_loader.sync <repo>") from e
    return f"code/{workload}/{sha}"


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python -m pod_loader.sync",
                                 description="publish a workload's code/ to object storage")
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--workload", default=None, help="defaults to the repo directory name")
    ap.add_argument("--rev", default=None, help="defaults to the git sha, or dev if dirty")
    ap.add_argument("--profile", default=None, help="object store profile")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    store = None
    if a.profile:
        from .objectstore import get_storage
        store = get_storage(a.profile)
    try:
        r = publish(a.repo, workload=a.workload, rev=a.rev, store=store,
                    dry_run=a.dry_run)
    except SyncError as e:
        print(f"error: {e}")
        return 1
    print(json.dumps(r, indent=2))
    if r["mutable"] and not a.dry_run:
        print("\nnote: published to a MUTABLE dev/ path because the tree is dirty or is "
              "not a git repo.\n      Commit to publish under an immutable revision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
