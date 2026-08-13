"""Push a workload's code to object storage — the fast loop CI is too slow for.

    python -m lingua_core.sync code/ --repo lingua-trainer            → code/<repo>/dev/
    python -m lingua_core.sync code/ --repo lingua-trainer --rev abc  → code/<repo>/abc/

## Two destinations, deliberately different

    code/<repo>/<sha>/   IMMUTABLE. Written by CI, never rewritten. A spec naming one
                         gets exactly the bytes that commit had — which is what makes
                         "which code ran" answerable after the fact.
    code/<repo>/dev/     MUTABLE. Written by this command, overwritten every push. For
                         the edit-run loop, where waiting on CI for a one-line change is
                         the wrong trade.

The danger of the mutable one is real and worth naming: pushing to `dev/` WHILE a job is
reading it can change code under a running job, and Python's lazy imports mean a module
loaded at stage 4 may differ from one loaded at stage 1. So `dev/` is for iterating, and
anything you want to reproduce should name a SHA.

## What is never uploaded

Keys and env files, by pattern rather than by hoping. A credential reaching the volume
would undo the property that no pod ever holds a secret — the pod reads mounted files and
holds nothing.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
from pathlib import Path

# Belt and braces with .gitignore: a file that never should have existed locally must still
# not be uploaded if it does.
SKIP = ("*.key", ".env", "*.pem", "*.pyc", ".DS_Store", "id_rsa*", "*.sqlite", "*.db")
SKIP_DIRS = ("__pycache__", ".git", ".venv", "node_modules", ".pytest_cache", "out")


def _skip(rel: Path) -> bool:
    if any(part in SKIP_DIRS for part in rel.parts):
        return True
    return any(fnmatch.fnmatch(rel.name, pat) for pat in SKIP)


def collect(root: Path) -> list[tuple[Path, str]]:
    """(local path, key suffix) for everything publishable under root."""
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if _skip(rel):
            continue
        out.append((p, rel.as_posix()))
    return out


def push(root: Path, *, repo: str, rev: str = "dev", profile: str | None = None,
         dry_run: bool = False) -> dict:
    from .objectstore import get_storage

    prefix = f"code/{repo}/{rev}"
    files = collect(root)
    total = sum(p.stat().st_size for p, _ in files)

    # A dry run is exactly the thing you reach for when you are NOT sure the store is
    # configured, so requiring credentials to preview would defeat its purpose.
    if dry_run:
        for _, k in files[:20]:
            print(f"  would upload {prefix}/{k}")
        if len(files) > 20:
            print(f"  … and {len(files) - 20} more")
        return {"dry_run": True, "files": len(files), "bytes": total, "prefix": prefix}

    st = get_storage(profile)
    if not st.available:
        raise SystemExit(
            "no object store configured. Set LINGUA_S3_* env or provide a key file — "
            "see lingua_core/objectstore.py")
    cfg = st.require()
    for p, k in files:
        st.client.put_object(Bucket=cfg.bucket, Key=f"{prefix}/{k}",
                             Body=p.read_bytes())
    if rev != "dev":
        st.client.put_object(Bucket=cfg.bucket, Key=f"code/{repo}/latest",
                             Body=rev.encode())
    return {"files": len(files), "bytes": total, "prefix": prefix,
            "bucket": cfg.bucket,
            "spec_hint": {"code": {"root": prefix, "rev": rev}}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", help="the code directory whose CONTENTS become the code root")
    ap.add_argument("--repo", default=None,
                    help="workload name; defaults to the parent directory's name")
    ap.add_argument("--rev", default="dev",
                    help="'dev' (mutable, default) or an immutable revision label")
    ap.add_argument("--profile", default=None, help="object-store profile")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    root = Path(a.path).resolve()
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")
    repo = a.repo or root.parent.name

    r = push(root, repo=repo, rev=a.rev, profile=a.profile, dry_run=a.dry_run)
    print(f"  {r['files']} files, {r['bytes']/1e6:.2f} MB → {r['prefix']}")
    if not a.dry_run:
        print(f"  spec:  \"code\": {{\"root\": \"{r['prefix']}\", \"rev\": \"{a.rev}\"}}")
        if a.rev == "dev":
            print("  note:  dev/ is MUTABLE — name a SHA for anything you want to reproduce")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
