"""Runtime estimator — decides LOCAL vs RUNPOD before anything is spent.

    python deploy/estimate.py --source openslr_108_mediaspeech_es --stages normalize,embed

Rule: anything over 15 minutes goes to the cloud. This makes that rule mechanical rather
than remembered, and it uses THROUGHPUT MEASURED ON THIS MACHINE rather than guesses.

Measured on the M-series laptop, 10 cores, 8 GB to the OrbStack VM:

    whisper small int8      32.0 min audio ->  177 s   =  10.8x realtime
    Silero VAD + ECAPA      32.0 min audio ->  100 s   =  19.2x realtime
    ffmpeg 16 kHz mono      ~200x realtime (I/O bound)
    prosody + sibilant      ~150x realtime
    wav2vec2 posteriors     NOT measured locally — published CPU figures are 1-3x realtime,
                            which is why this is the stage that justifies a GPU at all

GPU speedups are deliberately conservative. An estimate that oversells the cloud is worse
than one that oversells the laptop, because the laptop failure mode is "wait", and the
cloud failure mode is "pay for an idle pod".
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LOCAL_BUDGET_MINUTES = 15.0

# realtime factors: audio_seconds / wall_seconds. Higher is faster.
THROUGHPUT = {
    "normalize":  {"cpu": 191.0, "gpu": 191.0,  "note": "MEASURED: 341 s audio -> 1.78 s"},
    "vad":        {"cpu": 60.0,  "gpu": 120.0,  "note": "Silero, small model"},
    "transcribe": {"cpu": 10.8,  "gpu": 60.0,   "note": "MEASURED locally: 32 min -> 177 s"},
    "diarize":    {"cpu": 19.2,  "gpu": 90.0,   "note": "MEASURED locally: 32 min -> 100 s"},
    "embed":      {"cpu": 68.3,  "gpu": 200.0,  "note": "MEASURED: 341 s audio -> 4.99 s (ECAPA)"},
    "measure":    {"cpu": 150.0, "gpu": 150.0,  "note": "librosa, CPU-bound, GPU adds nothing"},
    "posteriors": {"cpu": 2.0,   "gpu": 40.0,   "note": "⭐ wav2vec2 — THE reason to rent a GPU"},
}

AUDIO_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a", ".ogg", ".opus"}


@dataclass
class Estimate:
    source: str
    files: int
    audio_hours: float
    bytes: int
    per_stage: dict
    total_cpu_minutes: float
    total_gpu_minutes: float

    @property
    def verdict(self) -> str:
        if self.total_cpu_minutes <= LOCAL_BUDGET_MINUTES:
            return "LOCAL"
        return "RUNPOD"

    def as_dict(self) -> dict:
        return {
            "source": self.source, "files": self.files,
            "audio_hours": round(self.audio_hours, 2),
            "gigabytes": round(self.bytes / 1e9, 2),
            "per_stage": self.per_stage,
            "total_cpu_minutes": round(self.total_cpu_minutes, 1),
            "total_gpu_minutes": round(self.total_gpu_minutes, 1),
            "local_budget_minutes": LOCAL_BUDGET_MINUTES,
            "verdict": self.verdict,
            "upload_gigabytes": round(self.bytes / 1e9, 2) if self.verdict == "RUNPOD" else 0,
        }


def probe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20)
        return float(out.stdout.strip() or 0.0)
    except Exception:
        return 0.0


def scan(root: Path, *, sample: int = 40) -> tuple[int, float, int]:
    """Count files and estimate total duration by sampling, not probing everything."""
    files = [p for p in root.rglob("*") if p.suffix.lower() in AUDIO_SUFFIXES]
    if not files:
        return 0, 0.0, 0
    total_bytes = sum(p.stat().st_size for p in files)

    step = max(1, len(files) // sample)
    probed = [(p, probe_duration(p)) for p in files[::step][:sample]]
    probed = [(p, d) for p, d in probed if d > 0]
    if not probed:
        return len(files), 0.0, total_bytes

    # Duration per byte is far more stable than duration per file across mixed formats.
    sec_per_byte = sum(d for _, d in probed) / sum(p.stat().st_size for p, _ in probed)
    return len(files), total_bytes * sec_per_byte, total_bytes


def estimate(source_root: Path, stages: list[str], *, source_id: str = "") -> Estimate:
    files, seconds, nbytes = scan(source_root)
    per_stage, cpu_total, gpu_total = {}, 0.0, 0.0
    for s in stages:
        t = THROUGHPUT.get(s)
        if not t:
            continue
        cpu_min = seconds / t["cpu"] / 60
        gpu_min = seconds / t["gpu"] / 60
        per_stage[s] = {"cpu_minutes": round(cpu_min, 1),
                        "gpu_minutes": round(gpu_min, 1),
                        "speedup": round(t["cpu"] and t["gpu"] / t["cpu"], 1),
                        "note": t["note"]}
        cpu_total += cpu_min
        gpu_total += gpu_min
    return Estimate(source_id or source_root.name, files, seconds / 3600, nbytes,
                    per_stage, cpu_total, gpu_total)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="deploy.estimate")
    ap.add_argument("--source", required=True, help="source id under corpus_data/raw/")
    ap.add_argument("--corpus", default="../corpus_data")
    ap.add_argument("--stages", default="normalize,embed",
                    help=f"comma list from {','.join(THROUGHPUT)}")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    root = Path(a.corpus) / "raw" / a.source
    if not root.exists():
        print(f"not found: {root}")
        return 1

    est = estimate(root, [s.strip() for s in a.stages.split(",")], source_id=a.source)
    d = est.as_dict()
    if a.json:
        print(json.dumps(d, indent=2))
        return 0

    print(f"\n  {est.source}")
    print(f"  {est.files:,} files · {d['audio_hours']} h audio · {d['gigabytes']} GB\n")
    print(f"  {'stage':<14}{'CPU min':>10}{'GPU min':>10}   note")
    print("  " + "-" * 78)
    for s, v in est.per_stage.items():
        print(f"  {s:<14}{v['cpu_minutes']:>10.1f}{v['gpu_minutes']:>10.1f}   {v['note'][:44]}")
    print("  " + "-" * 78)
    print(f"  {'TOTAL':<14}{d['total_cpu_minutes']:>10.1f}{d['total_gpu_minutes']:>10.1f}")
    print()
    if est.verdict == "LOCAL":
        print(f"  ✅ LOCAL — {d['total_cpu_minutes']:.0f} min is within the "
              f"{LOCAL_BUDGET_MINUTES:.0f} min budget")
        print(f"     docker compose run --rm pipeline ...")
    else:
        print(f"  ☁️  RUNPOD — {d['total_cpu_minutes']:.0f} min exceeds the "
              f"{LOCAL_BUDGET_MINUTES:.0f} min budget")
        print(f"     upload {d['upload_gigabytes']} GB · ~{d['total_gpu_minutes']:.0f} min on GPU")
        print(f"     ./deploy/runpod.sh sync {est.source}")
        print(f"     ./deploy/runpod.sh run  {est.source} --stages {a.stages}")
        print(f"     ./deploy/runpod.sh fetch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
