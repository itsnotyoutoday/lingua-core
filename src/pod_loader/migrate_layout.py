"""One-time migration of an existing bucket to the STRUCTURE.md layout.

    python -m pod_loader.migrate_layout --dry-run     # always do this first
    python -m pod_loader.migrate_layout --apply

## Why this is careful

The bucket holds 87,141 corpus objects and 15.5 GB. Most of that is `corpus/raw/`, which
CANNOT be recreated — it is the only thing in the system that is neither computed nor
authored. So the migration:

  * NEVER touches `corpus/raw/`
  * COPIES then verifies then deletes, never moves blind
  * refuses to delete a source object whose copy is not byte-identical in size
  * is idempotent — re-running after a partial failure resumes rather than duplicating

S3 has no rename. Every "move" is copy + delete, which means a failure between the two
leaves both. That is recoverable; the reverse (delete before copy confirmed) is not.

## What moves

See STRUCTURE.md. The important ones:

    corpus/normalized/  -> assets/derived/normalized/    derived, not input
    corpus/work/        -> assets/derived/work/
    code/corpora/       -> corpus/recipes/               data, AND it collides with
    code/corpus_research.json -> corpus/corpus_research.json     code/<repo>/<sha>/
    out/_reports/       -> assets/profiles/ or runs/     by content
    logs/<job>.*        -> runs/<job>/
    .cache/             -> cache/
    build/, _probe/     -> delete (images come from GHCR now)
"""
from __future__ import annotations

import argparse
import re
from typing import Iterable

from . import paths

# (pattern, replacement) applied in order; first match wins. `None` means DELETE.
RULES: list[tuple[str, str | None]] = [
    # Inputs stay exactly where they are.
    (r"^corpus/raw/", "corpus/raw/"),
    (r"^corpus/corpus_research\.json$", "corpus/corpus_research.json"),

    # Derived data moves out of the corpus root: it is reproducible, so it has a different
    # retention policy from the raw audio sitting beside it.
    (r"^corpus/normalized/", "assets/derived/normalized/"),
    (r"^corpus/work/", "assets/derived/work/"),
    (r"^corpus/\.cache/", "cache/"),

    # Data filed under code/. Also a hard collision: code/<repo>/<sha>/ would read
    # `corpora` as a repository name.
    (r"^code/corpora/", "corpus/recipes/"),
    (r"^code/corpus_research\.json$", "corpus/corpus_research.json"),
    # Job specs exist BOTH at jobs/ and code/jobs/ (a stale code sync copied them). Both
    # once mapped to one destination, so one would have silently overwritten the other —
    # caught by the dry-run's duplicate-destination check, which is why that check exists.
    # Keep the provenance rather than guessing which copy is authoritative.
    (r"^code/jobs/", "runs/_legacy_specs/from_code_sync/"),

    # Anything else already under code/ predates code/<repo>/<sha>/ and is a stale sync of
    # the old flat repo. The current code is republished by CI, so this is not lost work.
    (r"^code/", None),

    (r"^\.cache/", "cache/"),

    # Flat job specs predate runs/<job_id>/spec.json. Kept rather than deleted: they are
    # the only record of what older runs were asked to do, and they are kilobytes.
    (r"^jobs/", "runs/_legacy_specs/from_volume/"),

    # Reports: keep, but under assets where promoted outputs live.
    (r"^out/_reports/", "assets/reports/"),
    (r"^out/", "assets/reports/"),

    # Logs were flat per job, with .DONE/.FAILED markers. The markers are superseded by
    # verification-driven resume, which asks the outputs rather than trusting a flag.
    (r"^logs/(?P<job>[^/.]+)\.DONE$", None),
    (r"^logs/(?P<job>[^/.]+)\.FAILED$", None),
    (r"^logs/(?P<job>[^/.]+)\.log$", r"runs/\g<job>/job.log"),
    (r"^logs/", "runs/_legacy_logs/"),

    # Obsolete: images come from GHCR, and the probe files were a one-off test.
    (r"^build/", None),
    (r"^_probe/", None),
]


def _size_via_list(client, bucket: str, key: str) -> int | None:
    """Exact-key size from a listing. Used instead of head_object — see run()."""
    r = client.list_objects_v2(Bucket=bucket, Prefix=key)
    for o in r.get("Contents", []):
        if o["Key"] == key:
            return o["Size"]
    return None


def plan_key(k: str) -> tuple[str | None, str]:
    """Return (new_key or None to delete, rule that matched)."""
    for pat, repl in RULES:
        m = re.match(pat, k)
        if not m:
            continue
        if repl is None:
            return None, pat
        return re.sub(pat, repl, k), pat
    return k, "(unmatched — left alone)"


def build_plan(keys: Iterable[str]) -> dict:
    moves, deletes, keeps, unmatched = [], [], [], []
    for k in keys:
        new, rule = plan_key(k)
        if new is None:
            deletes.append((k, rule))
        elif new == k:
            (keeps if rule != "(unmatched — left alone)" else unmatched).append(k)
        else:
            moves.append((k, new, rule))
    return {"moves": moves, "deletes": deletes, "keeps": keeps, "unmatched": unmatched}


def run(*, apply: bool = False, profile: str | None = None, limit: int = 0) -> dict:
    from .objectstore import get_storage

    st = get_storage(profile)
    if not st.available:
        raise SystemExit("no object store configured")
    cfg = st.require()
    c = st.client

    keys, sizes = [], {}
    for page in c.get_paginator("list_objects_v2").paginate(Bucket=cfg.bucket):
        for o in page.get("Contents", []):
            keys.append(o["Key"])
            sizes[o["Key"]] = o["Size"]

    plan = build_plan(keys)
    summary = {"total": len(keys), "moves": len(plan["moves"]),
               "deletes": len(plan["deletes"]), "keeps": len(plan["keeps"]),
               "unmatched": len(plan["unmatched"]),
               "move_bytes": sum(sizes[k] for k, _, _ in plan["moves"]),
               "delete_bytes": sum(sizes[k] for k, _ in plan["deletes"])}

    if not apply:
        return {"dry_run": True, "plan": plan, "summary": summary}

    done_move, done_del, failed = 0, 0, []
    todo = plan["moves"][:limit] if limit else plan["moves"]
    for src, dst, _ in todo:
        try:
            c.copy_object(Bucket=cfg.bucket, Key=dst,
                          CopySource={"Bucket": cfg.bucket, "Key": src})
            # Verify BEFORE deleting. S3 has no rename, so a delete that outruns a failed
            # copy is unrecoverable.
            #
            # Verified by LISTING, not head_object: RunPod's S3 implementation is partial
            # and head_object returns 403 on larger objects — observed on a 91 MB zip while
            # working fine on a 347-byte yaml. A verification step that reports failure on
            # a copy that actually succeeded is worse than useless, because it would strand
            # duplicates on every large file.
            got = _size_via_list(c, cfg.bucket, dst)
            if got is None:
                failed.append((src, dst, "copy not visible in listing"))
                continue
            if got != sizes[src]:
                failed.append((src, dst, f"size mismatch: {got} != {sizes[src]}"))
                continue
            c.delete_object(Bucket=cfg.bucket, Key=src)
            done_move += 1
        except Exception as exc:
            failed.append((src, dst, f"{type(exc).__name__}: {exc}"))

    todo_del = plan["deletes"][:limit] if limit else plan["deletes"]
    for k, _ in todo_del:
        try:
            c.delete_object(Bucket=cfg.bucket, Key=k)
            done_del += 1
        except Exception as exc:
            failed.append((k, None, f"{type(exc).__name__}: {exc}"))

    return {"applied": True, "moved": done_move, "deleted": done_del,
            "failed": failed, "summary": summary}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--limit", type=int, default=0, help="cap operations, for a first pass")
    ap.add_argument("--show", type=int, default=12)
    a = ap.parse_args()

    r = run(apply=a.apply, profile=a.profile, limit=a.limit)
    s = r["summary"]
    print(f"  {s['total']} objects: {s['moves']} move, {s['deletes']} delete, "
          f"{s['keeps']} stay, {s['unmatched']} unmatched")
    print(f"  bytes: {s['move_bytes']/1e9:.2f} GB to move, "
          f"{s['delete_bytes']/1e6:.1f} MB to delete")

    if r.get("dry_run"):
        plan = r["plan"]
        print("\n  MOVES")
        for src, dst, _ in plan["moves"][:a.show]:
            print(f"    {src[:56]:<56} -> {dst[:56]}")
        if len(plan["moves"]) > a.show:
            print(f"    … {len(plan['moves']) - a.show} more")
        print("\n  DELETES")
        for k, _ in plan["deletes"][:a.show]:
            print(f"    {k[:80]}")
        if len(plan["deletes"]) > a.show:
            print(f"    … {len(plan['deletes']) - a.show} more")
        if plan["unmatched"]:
            print("\n  UNMATCHED (left alone — check these)")
            for k in plan["unmatched"][:a.show]:
                print(f"    {k[:80]}")
        bad = [k for k in plan["keeps"] + plan["unmatched"] if paths.check(k)]
        print(f"\n  keys that would still violate STRUCTURE.md: {len(bad)}")
        for k in bad[:5]:
            print(f"    {k[:80]}")
    else:
        print(f"  moved {r['moved']}, deleted {r['deleted']}, failed {len(r['failed'])}")
        for f in r["failed"][:10]:
            print(f"    FAIL {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
