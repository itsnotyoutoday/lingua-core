"""What an object store can actually do — because "S3-compatible" is a marketing claim.

## Why this exists

RunPod's object endpoint was configured through `LINGUA_S3_*` variables and handed to
boto3, which made every caller assume the S3 API. It is not the S3 API. Verified against
the live endpoint, not read from a doc:

    presigned URLs      unsupported            the generated URL is rejected
    delete_objects      HTTP 307               batch delete redirects and fails
    head_object         403 on a 91 MB object  works on small ones, so it looks fine in tests

That last one is the dangerous shape: it works until the object is big, so a size check
passes in development and fails on real data. Code that assumed S3 semantics discovered
the truth as a stack trace mid-job.

Cloudflare R2 is a real implementation — presigned URLs verified working — with one quirk
of its own: the signing region must be `auto` or requests are rejected.

So the fix is not to rename variables and move on. It is to make the *difference* legible:
a store says what it supports, callers ask, and the answer is a documented fact rather
than a runtime surprise. `store.supports("presigned")` is a question with an answer;
calling `generate_presigned_url()` and hoping is not.

## Why capability flags rather than try/except

Both work at runtime. Only one works at PLAN time. The control plane decides where a job
runs before spending a pod, and "this pipeline needs presigned URLs, so it cannot use the
RunPod store" is a decision worth making on a laptop in a millisecond rather than fifteen
minutes into a billed run. Handing a caller an accurate capability set is what makes the
provider genuinely swappable instead of nominally swappable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Every capability this codebase actually branches on. Deliberately small: a flag nobody
#: reads is a flag nobody maintains, and a stale capability table is worse than none.
CAPABILITIES = (
    "presigned",        # generate_presigned_url produces a URL that works
    "batch_delete",     # delete_objects with more than one key
    "head_large",       # head_object on objects over ~50 MB
    "multipart",        # multipart upload
    "versioning",       # object versions
    "list_v2",          # ListObjectsV2 pagination
)


@dataclass(frozen=True)
class Flavor:
    """A store implementation and its verified limits.

    `notes` carries WHY a capability is false, because a bare `False` invites someone to
    try it again in six months and rediscover the same 307.
    """
    name: str
    true_s3: bool
    supported: frozenset
    signing_region: str | None = None
    notes: dict = field(default_factory=dict)

    def supports(self, capability: str) -> bool:
        if capability not in CAPABILITIES:
            raise ValueError(
                f"unknown capability {capability!r}; known: {', '.join(CAPABILITIES)}")
        return capability in self.supported

    def why_not(self, capability: str) -> str:
        return self.notes.get(capability, "not supported by this store")

    def require(self, *capabilities: str) -> None:
        """Fail before the work starts, naming the store and the missing capability."""
        missing = [c for c in capabilities if not self.supports(c)]
        if missing:
            lines = [f"store {self.name!r} does not support: {', '.join(missing)}"]
            lines += [f"  {c}: {self.why_not(c)}" for c in missing]
            lines.append("  Use a fully S3-compatible store (R2, AWS, MinIO) for this job.")
            raise UnsupportedCapability("\n".join(lines))

    def describe(self) -> dict:
        return {"name": self.name, "true_s3": self.true_s3,
                "supports": {c: self.supports(c) for c in CAPABILITIES},
                "notes": self.notes}


class UnsupportedCapability(RuntimeError):
    """Raised at plan time, not discovered at run time."""


_ALL = frozenset(CAPABILITIES)

RUNPOD = Flavor(
    name="runpod", true_s3=False,
    supported=_ALL - {"presigned", "batch_delete", "head_large", "versioning"},
    notes={
        "presigned": "RunPod rejects presigned URLs; verified against the live endpoint",
        "batch_delete": "delete_objects returns HTTP 307 — delete one key at a time",
        "head_large": "head_object returns 403 on large objects (seen at 91 MB) while "
                      "succeeding on small ones, so this fails only on real data",
        "versioning": "not offered",
    })

R2 = Flavor(
    name="r2", true_s3=True, supported=_ALL, signing_region="auto",
    notes={"_region": "the signing region must be 'auto'; anything else is rejected"})

AWS = Flavor(name="aws", true_s3=True, supported=_ALL)
MINIO = Flavor(name="minio", true_s3=True, supported=_ALL - {"versioning"},
               notes={"versioning": "depends on deployment; assumed off"})

#: An unknown endpoint is assumed fully capable. Assuming LESS would refuse to run against
#: a perfectly good store nobody has classified yet; assuming more merely produces the same
#: runtime error you would have had without this module.
UNKNOWN = Flavor(name="unknown", true_s3=True, supported=_ALL,
                 notes={"_": "unclassified endpoint — capabilities assumed, not verified"})

_BY_HOST = (
    ("runpod", RUNPOD),
    ("r2.cloudflarestorage.com", R2),
    ("amazonaws.com", AWS),
    ("minio", MINIO),
)


def flavor_for(endpoint_url: str | None) -> Flavor:
    """Classify a store by endpoint. Hostname is the only thing available before a call."""
    host = (endpoint_url or "").lower()
    if not host:
        return AWS                      # no endpoint override means real AWS
    for needle, flavor in _BY_HOST:
        if needle in host:
            return flavor
    return UNKNOWN


def for_store(store) -> Flavor:
    """Classify an already-built Storage."""
    cfg = getattr(store, "config", None) or getattr(store, "cfg", None)
    return flavor_for(getattr(cfg, "endpoint_url", None) if cfg else None)


if __name__ == "__main__":                            # python -m pod_loader.capabilities
    import json
    print(json.dumps({f.name: f.describe() for f in (RUNPOD, R2, AWS, MINIO)}, indent=2))
