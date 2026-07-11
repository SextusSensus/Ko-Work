#!/usr/bin/env python3
"""jobspec.py -- the job spec + capability matching for the LAN job-pool (docs/CLUSTER_PLAN.md).

ONE definition of "a job" shared by the scheduler, workers, and the submit CLI. Stdlib only, so it
imports anywhere (laptop stubs, any worker, the coordinator). A job is a plain JSON object on disk in
the filesystem queue (cluster/jobqueue.py); this module is its schema, (de)serialization, validation, and
the pure capability-match predicate the scheduler uses to assign a job to a worker.

Authored blind on a no-Python laptop -- VERIFY ON CLUSTER. Self-test needs no network/Docker/GPU:
  python -m cluster.jobspec selftest   ->  JOBSPEC-SELFTEST-OK
(or: python cluster/jobspec.py selftest)

Design contracts (CLUSTER_PLAN.md sections 3-5):
  * job_id = UTC stamp + short hash, lexically sortable by creation time (like offload_run.sh run_id).
  * states: queued -> leased -> running -> done | failed  (only these; transitions enforced in queue.py).
  * requires = {backend, min_vram_gb, min_ram_gb}; a worker's advertised caps must satisfy ALL of them.
  * a job never mutates its input bundle; artifacts push back to runs/<run_id>/<type>/ on the store.
  * provenance: image_digest (the recon_version doctrine, generalized to job_version) + seed travel
    with the result so any worker reproduces to the gate's tolerance.
"""
import hashlib
import json
import sys

# --- frozen vocab (bump deliberately; workers/scheduler share this exact set) --------------------
JOB_TYPES = ("ingest", "recon", "train", "splat",
             "detect", "depth", "segment")           # npu/infer.py offline-inference jobs (backend=npu)
BACKENDS = ("cpu", "cuda", "mps", "npu")          # a job requires ONE; a worker advertises a SET
STATES = ("queued", "leased", "running", "done", "failed")
TERMINAL_STATES = ("done", "failed")

# state -> the states it may transition to (enforced by queue.py; listed here as the single source)
ALLOWED_TRANSITIONS = {
    "queued": ("leased",),
    "leased": ("running", "failed", "queued"),   # queued again == lease-timeout requeue
    "running": ("done", "failed"),
    "done": (),
    "failed": ("queued",),                        # a retry re-queues (queue.py bounds attempts)
}

_REQUIRE_KEYS = ("backend", "min_vram_gb", "min_ram_gb")


def make_job_id(now, salt):
    """Deterministic, sortable job id from a caller-supplied clock + salt (NEVER a hidden time()/random
    -- the caller owns those so the self-test is reproducible). `now` = a UTC struct/epoch-derived
    string 'YYYYmmddTHHMMSS'; `salt` = anything unique-ish (pid, counter). Returns e.g.
    '20260710T143002-9f3c1a'. Mirrors offload_run.sh's timestamp+shortsha run_id shape so job ids and
    run ids read alike."""
    h = hashlib.sha256(("%s|%s" % (now, salt)).encode("utf-8")).hexdigest()[:6]
    return "%s-%s" % (now, h)


def new_job(job_id, job_type, image, inputs, requires=None, entrypoint=None, args=None,
            artifacts_out=None, priority=100, seed=0):
    """Build a fresh queued job dict. Validates eagerly (raises ValueError) so a malformed job can
    never reach the queue. priority: LOWER runs first (100 default); ties broken by job_id (== creation
    order). requires defaults to {backend: cpu} -- the safest, most-schedulable class."""
    job = {
        "schema": 1,
        "job_id": str(job_id),
        "type": str(job_type),
        "image": str(image),
        "entrypoint": entrypoint,                 # None -> the image's ENTRYPOINT
        "args": list(args or []),
        "inputs": list(inputs or []),             # run_id strings the worker fetches from the store
        "requires": _norm_requires(requires),
        "artifacts_out": list(artifacts_out or []),
        "priority": int(priority),
        "seed": int(seed),
        "status": "queued",
        "worker_id": None,
        "attempts": 0,
        "image_digest": None,                     # filled by the worker at run time (job_version)
        "created_utc": None,                      # stamped by the caller/queue (kept out of validate)
        "result_manifest": None,                  # {status, artifacts:[{path,sha256,bytes}], metrics}
    }
    validate(job)
    return job


def _norm_requires(requires):
    r = dict(requires or {})
    out = {
        "backend": str(r.get("backend", "cpu")),
        "min_vram_gb": float(r.get("min_vram_gb", 0.0)),
        "min_ram_gb": float(r.get("min_ram_gb", 0.0)),
    }
    return out


def validate(job):
    """Raise ValueError (loud, specific) on any malformed job. Called on new_job, on every queue
    write, and by the scheduler on lease -- a bad job fails fast at the boundary, never mid-run."""
    if not isinstance(job, dict):
        raise ValueError("job must be a dict, got %r" % type(job).__name__)
    for k in ("job_id", "type", "image", "requires", "status", "priority", "attempts"):
        if k not in job:
            raise ValueError("job missing required key %r" % k)
    if not job["job_id"] or not isinstance(job["job_id"], str):
        raise ValueError("job_id must be a non-empty string")
    if job["type"] not in JOB_TYPES:
        raise ValueError("job %s: type %r not in %r" % (job["job_id"], job["type"], JOB_TYPES))
    if job["status"] not in STATES:
        raise ValueError("job %s: status %r not in %r" % (job["job_id"], job["status"], STATES))
    if not isinstance(job["image"], str) or not job["image"]:
        raise ValueError("job %s: image must be a non-empty string" % job["job_id"])
    req = job["requires"]
    if not isinstance(req, dict) or any(k not in req for k in _REQUIRE_KEYS):
        raise ValueError("job %s: requires must have keys %r" % (job["job_id"], _REQUIRE_KEYS))
    if req["backend"] not in BACKENDS:
        raise ValueError("job %s: requires.backend %r not in %r"
                         % (job["job_id"], req["backend"], BACKENDS))
    for k in ("min_vram_gb", "min_ram_gb"):
        if not isinstance(req[k], (int, float)) or req[k] < 0:
            raise ValueError("job %s: requires.%s must be a number >= 0" % (job["job_id"], k))
    if not isinstance(job["inputs"], list):
        raise ValueError("job %s: inputs must be a list" % job["job_id"])
    if int(job["attempts"]) < 0:
        raise ValueError("job %s: attempts must be >= 0" % job["job_id"])
    return job


def requires_satisfied_by(requires, worker_caps):
    """PURE predicate: can a worker advertising `worker_caps` run a job needing `requires`? True iff the
    worker lists the required backend AND meets the vram/ram floors. This is the whole scheduler match
    rule -- kept pure + here so it is trivially unit-testable and can never diverge between scheduler
    and any what-can-I-run check a worker does locally.

    worker_caps = {backends: [..], vram_gb: float, ram_gb: float}. A missing/empty backends list or a
    missing floor is treated as NOT satisfying (fail-closed: never dispatch to a worker that did not
    positively advertise the capability)."""
    req = _norm_requires(requires)
    caps = worker_caps or {}
    backends = caps.get("backends") or []
    if req["backend"] not in backends:
        return False
    if float(caps.get("vram_gb", 0.0)) < req["min_vram_gb"]:
        return False
    if float(caps.get("ram_gb", 0.0)) < req["min_ram_gb"]:
        return False
    return True


def dumps(job):
    """Canonical on-disk form: sort_keys + indent so a job file diffs cleanly and hashes stably."""
    return json.dumps(job, sort_keys=True, indent=2)


def loads(text):
    job = json.loads(text)
    return validate(job)


# --- self-test (no network / Docker / GPU) -------------------------------------------------------
def _selftest():
    jid = make_job_id("20260710T143002", "pid1234-0")
    assert jid == make_job_id("20260710T143002", "pid1234-0"), "job_id must be deterministic"
    assert jid.startswith("20260710T143002-") and len(jid.split("-")[1]) == 6

    j = new_job(jid, "recon", "k1recon:dev", inputs=["20260709T101010-abc123"],
                requires={"backend": "cpu"}, priority=50, seed=7)
    assert j["status"] == "queued" and j["attempts"] == 0 and j["seed"] == 7
    assert j["requires"] == {"backend": "cpu", "min_vram_gb": 0.0, "min_ram_gb": 0.0}

    # round-trip through the on-disk form is loss-less and re-validates
    j2 = loads(dumps(j))
    assert j2 == j, "dumps/loads must round-trip exactly"

    # capability matching -- the whole scheduler rule
    cpu_worker = {"backends": ["cpu"], "vram_gb": 0.0, "ram_gb": 64.0}
    cuda_worker = {"backends": ["cpu", "cuda"], "vram_gb": 10.0, "ram_gb": 64.0}
    assert requires_satisfied_by({"backend": "cpu"}, cpu_worker) is True
    assert requires_satisfied_by({"backend": "cuda"}, cpu_worker) is False   # no cuda backend
    assert requires_satisfied_by({"backend": "cuda", "min_vram_gb": 8}, cuda_worker) is True
    assert requires_satisfied_by({"backend": "cuda", "min_vram_gb": 16}, cuda_worker) is False  # vram floor
    assert requires_satisfied_by({"backend": "cpu", "min_ram_gb": 128}, cpu_worker) is False    # ram floor
    assert requires_satisfied_by({"backend": "cpu"}, {}) is False           # fail-closed on empty caps
    assert requires_satisfied_by({"backend": "cpu"}, {"backends": []}) is False

    # validation is loud + specific on each malformed field
    for bad in (
        {**j, "type": "notatype"},
        {**j, "status": "weird"},
        {**j, "image": ""},
        {**j, "requires": {"backend": "cpu"}},                # missing floor keys
        {**j, "requires": {"backend": "gpu", "min_vram_gb": 0, "min_ram_gb": 0}},  # bad backend
        {**j, "attempts": -1},
    ):
        try:
            validate(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("validate should have rejected: %r" % bad)

    # transition table is self-consistent (every target is a real state)
    for src, dsts in ALLOWED_TRANSITIONS.items():
        assert src in STATES
        for d in dsts:
            assert d in STATES, "%s -> %s targets unknown state" % (src, d)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "selftest":
        raise SystemExit("usage: python cluster/jobspec.py selftest")
    _selftest()
    print("JOBSPEC-SELFTEST-OK")
