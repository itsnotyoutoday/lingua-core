# pod-loader-rpc

Rent a pod, put your job on it, watch it work, and make certain it dies when it should.

The client side of [`pod-harness`](https://github.com/itsnotyoutoday/pod-harness). It shares
no code with it — the two agree on a versioned contract and talk over HTTP.

---

## Introduction

Running batch work on rented compute is three problems, not one:

1. **Getting the job there.** Your code, your spec and your data have to reach a machine
   that did not exist a minute ago — without putting credentials or a git token on it.
2. **Knowing what happened.** The provider offers no logs API. A pod is either watched live
   or it is a black box.
3. **Making sure it stops.** This is the one that costs money. An in-pod timeout **cannot
   terminate a RunPod pod** — `runpodctl` from inside returns `Unauthorized`. It detects the
   overrun, tries to self-delete, is refused, logs the refusal, and keeps billing. Ask how I
   know.

This handles all three. It provisions compute, publishes your code and spec to object
storage, starts the harness pointed at them, drives it over `/v1`, and guarantees
termination from outside the pod — where it actually works.

**Design constraints, and why**

- **The pod holds no credentials.** Code arrives as objects the pod can already read. No
  git access, no API key, nothing to leak if the pod is compromised.
- **The launcher assigns; workers never discover.** A worker that scans a queue can race
  another worker, and object storage cannot arbitrate. Every pod is told exactly what it
  owns at creation.
- **This package is never installed in a pod.** It can provision compute; a pod must not be
  able to. `pod-harness` fails its build if any of these modules appear inside it.

---

## Use

### Setup

```bash
pip install -e .

# credentials, outside any repo so they cannot be committed
cat > ~/runpod.key <<'EOF'
api_key = <your runpod key>
EOF

cat > ~/runpod-volume.key <<'EOF'
volume_id = <your network volume id>
EOF
```

`RUNPOD_VOLUME` is deliberately provider-named. A network volume is RunPod-specific, cannot
be emulated elsewhere, and **pins your compute to one datacentre** — worth seeing in a
launcher at a glance. Point it at a file, or set it inline; details come from the RunPod API
rather than a second copy in the file that would go stale.

### The CLI

```bash
python runctl.py --help

python runctl.py create   --provider runpod     # bring a runner into existence
python runctl.py status                         # one runner, or all of them
python runctl.py launch   --spec jobs/my.json   # sync code + spec, provision, run
python runctl.py watch    --job <job_id>        # status + live log
python runctl.py fetch    --job <job_id>        # pull artifacts back
python runctl.py kill                           # terminate (stopping alone still bills)
python runctl.py ls | cat | browse              # inspect the object store
```

### Publish your code

```bash
python -m pod_loader.sync ../my-workload
```

Uploads `<repo>/code/**` to `code/<workload>/<rev>/`, where `<rev>` is the git SHA — or
`dev` if the tree is dirty. That distinction matters: publishing to a mutable path can
change code under a **running** job, and Python's lazy imports would then mix two versions
inside one process. A dirty tree never publishes under a SHA.

### Validate before you spend

```python
from pod_loader import contract

contract.require_valid(spec)                                  # against the bundled copy
contract.require_valid(spec, contract.from_image(pod_url))    # against the running image
```

Prefer the second. It validates against the interface of the exact image that will run the
job. A typo'd stage name costs a millisecond here and the full image-pull-plus-boot on a
pod.

### Guaranteed termination

```python
from pod_loader import reaper

with reaper.pod(create_kwargs, budget_min=60) as pod_id:
    ...                     # terminated on success, exception, Ctrl-C or SIGTERM
```

Three layers, each covering the previous one's failure:

| layer | covers |
|---|---|
| context manager | normal exit, exceptions, signals |
| deadline thread | wall clock exceeded while the main thread is blocked on a hung call |
| `reaper.sweep()` | pods orphaned by a process that died before its `finally` could run |

Every launch is journaled to disk **before** the API call returns, so a pod whose creating
process vanished is still discoverable. A pod nobody journaled is a pod nobody can find.

> **Known gap.** All three layers need a process running on your machine. If your laptop
> sleeps with a pod up and nothing external is watching, nothing reaps it. A long-lived
> reaper service is the fix and is not built yet.

---

## Integration

### What crosses the boundary

Nothing but data. There is no function this package calls in the harness and none the
harness calls here. They agree on four things:

| | direction |
|---|---|
| job spec schema | loader writes, harness reads |
| event / status schema | harness writes, loader reads |
| environment variables | loader sets, harness consumes |
| `/v1` endpoints | loader polls |

All four live in `contract.json` in the harness repo and are served at `GET /v1/contract`.
Each side tests itself against the contract independently, so neither has to trust the
other — and unlike a shared library, a contract cannot be silently satisfied by a stale
cached copy.

### Composing a launch

```python
from pod_loader import contract, volume, reaper

vol = volume.require()                    # RUNPOD_VOLUME → id, datacentre, size
env = {
    "PODH_MODE": "batch",
    "PODH_JOB_ID": job_id,
    "PODH_JOB_SPEC": f"/workspace/{spec_key}",
    "PODH_LOG_ROOT": "/workspace/runs",
    "PODH_RUN_PREFIX": f"runs/{job_id}",
    "PODH_WRITE_PREFIXES": f"runs/{job_id},assets/",
}
assert not contract.check_env(env)        # every required variable present
```

Read the required set from the contract rather than hardcoding it — when the harness starts
needing a new root, launches fail at composition time instead of the pod refusing to boot.

### Adding a provider

`provider.py` defines the interface; `RunPodProvider` and `LocalProvider` implement it.
Adding a backend is a class, not a rewrite — everything above it speaks the same `/v1`.

### Storage layout

`paths.py` and `store.py` own the object-key layout, and the harness deliberately does not.
That knowledge used to live in both, and the copies drifted three times in one day. One
definition here; the harness is told, and is granted only the prefixes it may write to.

---

## Development

```bash
pip install -e ".[dev]"
python -m pod_loader.contract jobs/*.json     # validate specs offline
python -m pod_loader.volume                   # resolve and describe the volume
python -m pod_loader.sync ../workload --dry-run
```

## Licence

MIT.
