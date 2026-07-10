#!/usr/bin/env python3
"""submit.py -- enqueue a job into the pool (docs/CLUSTER_PLAN.md).

Runs ON THE COORDINATOR (the Python node that hosts the scheduler + queue), writing a job JSON into the
filesystem queue. The laptop (no Python) submits by ssh-ing to the coordinator and running this --
`desktop/Submit-Job.ps1` is that wrapper.

  python -m cluster.submit --queue ~/k1/queue --type recon --image k1recon:dev \
         --inputs 20260709T101010-abc123 --requires backend=cpu
  python -m cluster.submit selftest        ->  SUBMIT-SELFTEST-OK

Authored on the anaconda-python laptop and self-test-verified there; the live coordinator path is
VERIFY ON CLUSTER.
"""
import argparse
import os
import sys

try:
    from . import jobspec
    from .jobqueue import JobQueue
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import jobspec
    from jobqueue import JobQueue


def _parse_requires(pairs):
    """--requires backend=cuda min_vram_gb=8 -> {backend, min_vram_gb, min_ram_gb}. Unknown keys are a
    loud error (a typo'd requirement that silently no-ops would mis-route a job)."""
    req = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit("--requires expects key=value, got %r" % p)
        k, v = p.split("=", 1)
        if k not in ("backend", "min_vram_gb", "min_ram_gb"):
            raise SystemExit("--requires: unknown key %r (allowed: backend, min_vram_gb, min_ram_gb)" % k)
        req[k] = v if k == "backend" else float(v)
    return req


def build_job(now_str, salt, job_type, image, inputs, requires=None, entrypoint=None, args=None,
              priority=100, seed=0, artifacts_out=None):
    """Pure: build a validated queued job. now_str/salt are injected (the caller owns the clock) so the
    self-test is deterministic. new_job validates eagerly -> a malformed submit never reaches the queue."""
    job_id = jobspec.make_job_id(now_str, salt)
    job = jobspec.new_job(job_id, job_type, image, inputs, requires=requires, entrypoint=entrypoint,
                          args=args, priority=priority, seed=seed, artifacts_out=artifacts_out)
    job["created_utc"] = now_str
    return job


def _now_str():
    import time
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("enqueue", help="enqueue a job (default)")
    for name, kw in (("--queue", {"required": True, "help": "queue dir on the coordinator"}),
                     ("--type", {"required": True, "choices": jobspec.JOB_TYPES}),
                     ("--image", {"required": True})):
        s.add_argument(name, **kw)
    s.add_argument("--inputs", nargs="*", default=[], help="run_id(s) the worker fetches from the store")
    s.add_argument("--requires", nargs="*", default=["backend=cpu"], help="key=value capability floors")
    s.add_argument("--entrypoint", default=None)
    s.add_argument("--arg", dest="args", action="append", default=[], help="repeatable container arg")
    s.add_argument("--priority", type=int, default=100, help="lower runs first")
    s.add_argument("--seed", type=int, default=0)
    sub.add_parser("selftest")
    # default subcommand = enqueue (so `submit --queue ... --type ...` works without the word)
    if argv and argv[0] not in ("enqueue", "selftest", "-h", "--help"):
        argv = ["enqueue"] + argv
    a = ap.parse_args(argv)

    if a.cmd == "selftest":
        _selftest()
        print("SUBMIT-SELFTEST-OK")
        return 0
    if a.cmd != "enqueue":
        ap.print_help()
        return 2

    job = build_job(_now_str(), "pid%d" % os.getpid(), a.type, a.image, a.inputs,
                    requires=_parse_requires(a.requires), entrypoint=a.entrypoint, args=a.args,
                    priority=a.priority, seed=a.seed)
    q = JobQueue(a.queue)
    jid = q.enqueue(job)
    print("QUEUED %s  type=%s image=%s requires=%s inputs=%s"
          % (jid, job["type"], job["image"], job["requires"], job["inputs"]))
    return 0


def _selftest():
    import shutil
    import tempfile
    root = tempfile.mkdtemp(prefix="k1submit-")
    try:
        # build is deterministic given (now_str, salt); validation rejects a bad requirement key.
        j = build_job("20260710T140000Z", "pid1", "recon", "k1recon", ["run-A"],
                      requires={"backend": "cpu"}, priority=50, seed=3)
        assert j["job_id"] == jobspec.make_job_id("20260710T140000Z", "pid1")
        assert j["status"] == "queued" and j["type"] == "recon" and j["seed"] == 3
        try:
            _parse_requires(["backendd=cpu"])          # typo'd key must be loud
        except SystemExit:
            pass
        else:
            raise AssertionError("unknown --requires key should raise")
        assert _parse_requires(["backend=cuda", "min_vram_gb=8"]) == {"backend": "cuda", "min_vram_gb": 8.0}
        # enqueue lands it in the queue as 'queued'; the scheduler/worker path is covered elsewhere.
        q = JobQueue(os.path.join(root, "q"))
        q.enqueue(j)
        assert q.get(j["job_id"])["status"] == "queued"
        assert q.counts()["queued"] == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
