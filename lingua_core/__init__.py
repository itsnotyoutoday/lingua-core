"""lingua_core — loads jobs into the harness and drives them over RPC.

Provisions compute (RunPod today, another provider tomorrow), publishes the workload's
code and job spec, starts the harness pointed at them, then talks to it over /v1 until it
finishes — and guarantees it dies on budget whatever else happens.

It shares no code with the harness. The two agree on four things and nothing else:

    the job spec schema        what this package writes and the harness reads
    the event/status schema    what the harness writes and this package reads
    the environment variables  LINGUA_JOB_SPEC, LINGUA_LOG_ROOT, LINGUA_RUN_PREFIX, …
    the /v1 endpoints          what this package polls

That is deliberate. A shared library would not even guarantee agreement — a pip install
served a stale engine out of a layer cache exactly once, which is how the lesson was
learned — whereas a versioned contract can be tested from both sides independently.

This package also owns the storage LAYOUT (paths.py, store.py, STRUCTURE.md). The harness
does not, and must not: that knowledge lived in both repos and the copies drifted three
times in one day. One definition, here, passed to the harness as configuration.
"""
