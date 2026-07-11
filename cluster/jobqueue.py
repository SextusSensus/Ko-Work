#!/usr/bin/env python3
"""jobqueue.py -- the filesystem job queue for the LAN job-pool (docs/CLUSTER_PLAN.md).

NOTE: named jobqueue.py, NOT queue.py, on purpose -- a module named `queue` shadows Python's stdlib
`queue` (which concurrent.futures imports inside the scheduler's gRPC server), a real sys.path footgun.

YAGNI by design: a job is a JSON file; a job's STATE is which subdir it sits in
(root/{queued,leased,running,done,failed}/<job_id>.json). Transitions are atomic os.replace + remove.
No database, no broker -- filesystem + JSON, survives a scheduler restart by scanning the dirs. The
scheduler (cluster/scheduler.py) is the SOLE writer and serializes every op under one in-process lock,
so this needs no cross-process locking (a single scheduler is the CLUSTER_PLAN invariant).

Authored blind on a no-Python laptop -- VERIFY ON CLUSTER. Self-test needs no network/Docker/GPU:
  python -m cluster.jobqueue selftest   ->  QUEUE-SELFTEST-OK

Durability contract: "losing a job must be impossible" (mirrors offload_run.sh's never-lose-a-run
doctrine). A crash mid-transition can leave a job file in TWO state dirs; _reconcile() on init keeps
the MOST-ADVANCED state and drops the rest, and terminal jobs are never garbage-collected here (a
retention sweep is a separate, explicit concern).
"""
import os
import sys
import tempfile

try:
    from . import jobspec
except ImportError:                               # allow `python cluster/jobqueue.py` (no package context)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import jobspec

_STATE_DIRS = jobspec.STATES                       # ("queued","leased","running","done","failed")
# rank for _reconcile: a higher number is "more advanced"; a job seen in two dirs keeps the higher.
_ADVANCE_RANK = {"queued": 0, "leased": 1, "running": 2, "failed": 3, "done": 4}


class JobQueue:
    def __init__(self, root, max_attempts=3):
        self.root = os.path.abspath(root)
        self.max_attempts = int(max_attempts)
        for s in _STATE_DIRS:
            os.makedirs(os.path.join(self.root, s), exist_ok=True)
        self._reconcile()

    # ---- paths ----
    def _path(self, state, job_id):
        return os.path.join(self.root, state, job_id + ".json")

    def _find(self, job_id):
        """Return (state, path) for job_id, or (None, None). Source of truth is the filesystem."""
        for s in _STATE_DIRS:
            p = self._path(s, job_id)
            if os.path.isfile(p):
                return s, p
        return None, None

    # ---- durable read/write ----
    @staticmethod
    def _read(path):
        with open(path) as f:
            return jobspec.loads(f.read())         # validates on every read -- a corrupt job is loud

    def _atomic_write(self, state, job):
        """Write job into root/<state>/ atomically (temp in the SAME dir -> os.replace, same-filesystem
        rename is atomic on POSIX and Windows). Returns the destination path."""
        d = os.path.join(self.root, state)
        dst = os.path.join(d, job["job_id"] + ".json")
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(jobspec.dumps(job))
            os.replace(tmp, dst)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return dst

    def _move(self, job, from_state, to_state):
        """Transition a job's state: validate the transition, write to the new dir, drop the old file.
        Order (write-new THEN remove-old) is deliberate: a crash between leaves the job in both dirs,
        which _reconcile heals toward the MORE-ADVANCED state -- never a vanished job."""
        allowed = jobspec.ALLOWED_TRANSITIONS.get(from_state, ())
        if to_state not in allowed:
            raise ValueError("illegal transition %s -> %s for job %s (allowed: %r)"
                             % (from_state, to_state, job["job_id"], allowed))
        job["status"] = to_state
        jobspec.validate(job)
        self._atomic_write(to_state, job)
        old = self._path(from_state, job["job_id"])
        if os.path.exists(old):
            os.remove(old)
        return job

    # ---- operations (scheduler calls these under its lock) ----
    def enqueue(self, job, created_utc=None):
        """Add a fresh job (status must be 'queued'). created_utc is stamped here if the caller did not.
        Idempotent-safe: enqueuing an existing job_id is rejected loudly (never silently duplicated)."""
        jobspec.validate(job)
        if job["status"] != "queued":
            raise ValueError("enqueue expects status 'queued', got %r" % job["status"])
        state, _ = self._find(job["job_id"])
        if state is not None:
            raise ValueError("job %s already present in state %r" % (job["job_id"], state))
        if job.get("created_utc") is None:
            job["created_utc"] = created_utc
        self._atomic_write("queued", job)
        return job["job_id"]

    def lease(self, worker_id, worker_caps, now_epoch):
        """Assign the best QUEUED job this worker can run to it. 'Best' = lowest priority number, ties
        broken by job_id (creation order). Returns the leased job dict, or None if nothing matches.
        Stamps worker_id + lease_epoch + attempts+1 and moves queued -> leased. PURE match rule lives
        in jobspec.requires_satisfied_by (fail-closed on unadvertised capability)."""
        best = None
        qdir = os.path.join(self.root, "queued")
        for name in os.listdir(qdir):
            if not name.endswith(".json") or name.startswith(".tmp-"):
                continue
            job = self._read(os.path.join(qdir, name))
            if not jobspec.requires_satisfied_by(job["requires"], worker_caps):
                continue
            key = (job["priority"], job["job_id"])
            if best is None or key < best[0]:
                best = (key, job)
        if best is None:
            return None
        job = best[1]
        job["worker_id"] = worker_id
        job["attempts"] = int(job["attempts"]) + 1
        job["lease_epoch"] = float(now_epoch)
        return self._move(job, "queued", "leased")

    def mark_running(self, job_id, now_epoch=None):
        state, path = self._find(job_id)
        if state != "leased":
            raise ValueError("mark_running: job %s is %r, expected 'leased'" % (job_id, state))
        job = self._read(path)
        if now_epoch is not None:
            job["lease_epoch"] = float(now_epoch)   # refresh so a long run isn't reaped mid-flight
        return self._move(job, "leased", "running")

    def touch_lease(self, job_id, now_epoch):
        """Refresh a leased/running job's lease_epoch so a LONG job (a multi-minute recon/train) is not
        reaped as stale mid-run. Called on every worker heartbeat. No state change; a no-op (returns
        False) if the job is not currently leased/running."""
        state, path = self._find(job_id)
        if state not in ("leased", "running"):
            return False
        job = self._read(path)
        job["lease_epoch"] = float(now_epoch)
        self._atomic_write(state, job)              # same-state rewrite; no transition
        return True

    def complete(self, job_id, result_manifest=None):
        state, path = self._find(job_id)
        if state != "running":
            raise ValueError("complete: job %s is %r, expected 'running'" % (job_id, state))
        job = self._read(path)
        job["result_manifest"] = result_manifest
        return self._move(job, "running", "done")

    def fail(self, job_id, reason=None):
        """Fail a leased/running job. Requeues for another attempt if attempts < max_attempts, else
        moves to 'failed' (terminal). Returns (job, requeued: bool)."""
        state, path = self._find(job_id)
        if state not in ("leased", "running"):
            raise ValueError("fail: job %s is %r, expected leased/running" % (job_id, state))
        job = self._read(path)
        job["last_error"] = reason
        if int(job["attempts"]) < self.max_attempts:
            job["worker_id"] = None
            job.pop("lease_epoch", None)
            self._move(job, state, "failed")        # failed is the only legal hop from running...
            # ...then failed -> queued is the sanctioned retry edge (bounded above).
            job2 = self._read(self._path("failed", job_id))
            return self._move(job2, "failed", "queued"), True
        return self._move(job, state, "failed"), False

    def requeue_stale(self, now_epoch, lease_timeout_s):
        """Reap leases whose worker went silent: any leased/running job whose lease_epoch is older than
        lease_timeout_s is failed-then-requeued (or terminally failed if attempts are exhausted). This
        is how a died/network-partitioned worker never wedges a job. Returns list of (job_id, requeued)."""
        out = []
        for state in ("leased", "running"):
            d = os.path.join(self.root, state)
            for name in list(os.listdir(d)):
                if not name.endswith(".json") or name.startswith(".tmp-"):
                    continue
                job = self._read(os.path.join(d, name))
                age = float(now_epoch) - float(job.get("lease_epoch", 0.0))
                if age >= lease_timeout_s:
                    _, requeued = self.fail(job["job_id"],
                                            "lease timeout (%.0fs idle)" % age)
                    out.append((job["job_id"], requeued))
        return out

    # ---- inspection ----
    def get(self, job_id):
        state, path = self._find(job_id)
        return self._read(path) if path else None

    def list(self, state=None):
        states = (state,) if state else _STATE_DIRS
        jobs = []
        for s in states:
            d = os.path.join(self.root, s)
            for name in sorted(os.listdir(d)):
                if name.endswith(".json") and not name.startswith(".tmp-"):
                    jobs.append(self._read(os.path.join(d, name)))
        return jobs

    def counts(self):
        return {s: sum(1 for n in os.listdir(os.path.join(self.root, s))
                       if n.endswith(".json") and not n.startswith(".tmp-"))
                for s in _STATE_DIRS}

    # ---- crash recovery ----
    def _reconcile(self):
        """A crash mid-transition can leave one job_id in two state dirs. Keep the MOST-ADVANCED copy
        (done > failed > running > leased > queued) and delete the rest -- a job is never lost, and a
        half-applied transition resolves forward, not backward."""
        seen = {}                                   # job_id -> (rank, state, path)
        for s in _STATE_DIRS:
            d = os.path.join(self.root, s)
            for name in os.listdir(d):
                if not name.endswith(".json") or name.startswith(".tmp-"):
                    if name.startswith(".tmp-"):    # orphaned temp from a crash mid-write
                        try:
                            os.remove(os.path.join(d, name))
                        except OSError:
                            pass
                    continue
                jid = name[:-5]
                rank = _ADVANCE_RANK[s]
                prev = seen.get(jid)
                if prev is None or rank > prev[0]:
                    if prev is not None:
                        try:
                            os.remove(prev[2])
                        except OSError:
                            pass
                    seen[jid] = (rank, s, os.path.join(d, name))
                else:
                    try:
                        os.remove(os.path.join(d, name))
                    except OSError:
                        pass


# --- self-test (no network / Docker / GPU) -------------------------------------------------------
def _selftest():
    import shutil
    root = tempfile.mkdtemp(prefix="k1q-")
    try:
        q = JobQueue(root, max_attempts=2)
        assert q.counts() == {"queued": 0, "leased": 0, "running": 0, "done": 0, "failed": 0}

        cpu = {"backends": ["cpu"], "vram_gb": 0.0, "ram_gb": 64.0}
        cuda = {"backends": ["cpu", "cuda"], "vram_gb": 10.0, "ram_gb": 64.0}

        # enqueue three jobs; a cuda-only job a cpu worker cannot take, and a high-priority cpu job.
        j_recon = jobspec.new_job("20260710T100000-aaa111", "recon", "k1recon", ["run-A"],
                                  requires={"backend": "cpu"}, priority=100)
        j_train = jobspec.new_job("20260710T100001-bbb222", "train", "k1train", ["ds-1"],
                                  requires={"backend": "cuda", "min_vram_gb": 8}, priority=50)
        j_hot = jobspec.new_job("20260710T100002-ccc333", "ingest", "k1ingest", ["run-A"],
                                requires={"backend": "cpu"}, priority=10)
        for j in (j_recon, j_train, j_hot):
            q.enqueue(j, created_utc="20260710T100000")
        assert q.counts()["queued"] == 3
        # duplicate enqueue is rejected
        try:
            q.enqueue(j_recon)
        except ValueError:
            pass
        else:
            raise AssertionError("duplicate enqueue should raise")

        # a cpu worker leases the lowest-priority-number cpu job (j_hot @10), not the cuda job.
        leased = q.lease("w-cpu", cpu, now_epoch=1000.0)
        assert leased["job_id"] == j_hot["job_id"], leased["job_id"]
        assert leased["status"] == "leased" and leased["worker_id"] == "w-cpu" and leased["attempts"] == 1
        # the cuda job is NOT leasable by the cpu worker; only the cuda worker gets it.
        assert q.lease("w-cpu", cpu, 1001.0)["job_id"] == j_recon["job_id"]   # next cpu job
        got_train = q.lease("w-cuda", cuda, 1002.0)
        assert got_train["job_id"] == j_train["job_id"]
        assert q.lease("w-cpu", cpu, 1003.0) is None                          # nothing left queued

        # happy path: leased -> running -> done, result recorded
        q.mark_running(j_hot["job_id"], now_epoch=1010.0)
        q.complete(j_hot["job_id"], result_manifest={"status": "ok", "artifacts": []})
        assert q.get(j_hot["job_id"])["status"] == "done"
        assert q.get(j_hot["job_id"])["result_manifest"]["status"] == "ok"

        # failure with retries: attempts=1 -> fail requeues (attempts already 1 < max 2) -> lease again
        # -> attempts=2 -> fail is now terminal.
        job, requeued = q.fail(j_recon["job_id"], "boom")
        assert requeued is True and job["status"] == "queued" and job["worker_id"] is None
        again = q.lease("w-cpu", cpu, 1020.0)
        assert again["job_id"] == j_recon["job_id"] and again["attempts"] == 2
        q.mark_running(j_recon["job_id"], 1021.0)
        # touch_lease refreshes a running job's lease (long-job keep-alive); no-op on a terminal job.
        assert q.touch_lease(j_recon["job_id"], 1022.0) is True
        assert q.get(j_recon["job_id"])["lease_epoch"] == 1022.0
        assert q.touch_lease(j_hot["job_id"], 9999.0) is False        # j_hot is done -> no-op
        job, requeued = q.fail(j_recon["job_id"], "boom again")
        assert requeued is False and job["status"] == "failed"

        # stale-lease reaping: the cuda job is still leased at t=1002; reap with a short timeout.
        stale = q.requeue_stale(now_epoch=1002.0 + 999.0, lease_timeout_s=60.0)
        assert (j_train["job_id"], True) in stale, stale
        assert q.get(j_train["job_id"])["status"] == "queued"

        # crash-reconcile: simulate a half-applied move (job present in BOTH queued and running).
        q.enqueue(jobspec.new_job("20260710T100003-ddd444", "ingest", "k1ingest", [],
                                  requires={"backend": "cpu"}))
        import shutil as _sh
        _sh.copy(q._path("queued", "20260710T100003-ddd444"),
                 q._path("running", "20260710T100003-ddd444"))
        q2 = JobQueue(root, max_attempts=2)          # re-init triggers _reconcile
        st, _ = q2._find("20260710T100003-ddd444")
        assert st == "running", "reconcile must keep the more-advanced state, got %r" % st
        assert not os.path.exists(q._path("queued", "20260710T100003-ddd444"))
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "selftest":
        raise SystemExit("usage: python cluster/jobqueue.py selftest")
    _selftest()
    print("QUEUE-SELFTEST-OK")
