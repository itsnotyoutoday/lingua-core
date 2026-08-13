"""lingua_core — the pipeline engine and pod control library.

Two things live here, and the split matters:

    the STAGE MODEL      framework.py — Stage, Runner, Context, Verification
                         execute_job.py, resume.py, progress.py, spec.py
    POD CONTROL          provider.py, runpod_api.py, batch_pod.py, reaper.py,
                         executor.py, browse.py, registry.py, dispatch.py

A workload repo supplies stage IMPLEMENTATIONS and imports this for everything else.
See README.md for how to build one.
"""
__version__ = "0.1.0"
