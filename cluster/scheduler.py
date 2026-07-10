#!/usr/bin/env python3
"""scheduler.py -- the single job-pool scheduler (docs/CLUSTER_PLAN.md).

Two layers, deliberately split so the logic self-tests with NO gRPC socket:
  * SchedulerCore -- pure logic over cluster/jobqueue.py + an in-memory worker registry. All the
    scheduling decisions live here; it is what the self-test drives with a fake worker.
  * serve() -- a thin gRPC server that translates cluster.proto RPCs onto SchedulerCore. It imports
    grpc + the generated cluster_pb2 stubs LAZILY (only when actually serving), so the self-test and
    any importer on the no-Python-grpc laptop never need them.

Single scheduler = the CLUSTER_PLAN invariant, so SchedulerCore serializes every mutation under one
lock; the queue's filesystem is the durable state (a scheduler restart rebuilds the registry from
workers re-registering, and the queue from its dirs). Coordinator host = the laptop (source of truth);
the queue dir lives beside runs/.

Authored blind on a no-Python laptop -- VERIFY ON CLUSTER. Self-test needs no network/Docker/GPU:
  python -m cluster.scheduler selftest   ->  SCHEDULER-SELFTEST-OK
"""
import os
import sys
import threading

try:
    from . import jobspec
    from .jobqueue import JobQueue
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import jobspec
    from jobqueue import JobQueue

DEFAULT_LEASE_TIMEOUT_S = 120


class SchedulerCore:
    """All scheduling logic; gRPC-free so it is unit-testable. Every public method takes an explicit
    `now` epoch (the caller owns the clock -> deterministic tests; the gRPC layer passes time.time())."""

    def __init__(self, queue, lease_timeout_s=DEFAULT_LEASE_TIMEOUT_S):
        self.q = queue
        self.lease_timeout_s = int(lease_timeout_s)
        self._lock = threading.Lock()
        self._workers = {}                          # worker_id -> {caps, last_seen, hostname}

    def register(self, worker_id, caps, now, hostname=""):
        """A worker announces itself + capabilities. Returns the lease timeout it must heartbeat within.
        caps = {backends:[...], vram_gb, ram_gb, images:[...]}."""
        if not worker_id:
            raise ValueError("register: empty worker_id")
        with self._lock:
            self._workers[worker_id] = {"caps": dict(caps or {}), "last_seen": float(now),
                                        "hostname": hostname}
        return self.lease_timeout_s

    def lease_job(self, worker_id, caps, now):
        """Reap stale leases first (so a died worker's job returns to the pool), refresh this worker's
        liveness + caps, then hand it the best job it can run. Returns a job dict or None."""
        with self._lock:
            self.q.requeue_stale(now, self.lease_timeout_s)
            w = self._workers.get(worker_id)
            if w is None:                            # a lease before register (scheduler restarted) --
                self._workers[worker_id] = w = {"caps": {}, "last_seen": float(now), "hostname": ""}
            if caps:
                w["caps"] = dict(caps)               # caps can change (an image was pulled) -> honor latest
            w["last_seen"] = float(now)
            return self.q.lease(worker_id, w["caps"], now)

    def report_result(self, worker_id, job_id, status, result_manifest=None, error=None, now=None):
        """A worker reports on a leased job. status: 'running' (progress -> refresh lease), 'done'
        (-> complete), 'failed' (-> fail/requeue). Guards that the reporter actually holds the lease --
        a stale worker reporting on a job that was reaped + reassigned is ignored loudly, never allowed
        to clobber the new lease-holder's result."""
        with self._lock:
            job = self.q.get(job_id)
            if job is None:
                return {"ok": False, "message": "no such job %s" % job_id}
            holder = job.get("worker_id")
            if holder is not None and worker_id and holder != worker_id:
                return {"ok": False, "message": "job %s held by %r not %r (ignored)"
                        % (job_id, holder, worker_id)}
            if status == "running":
                if job["status"] == "leased":
                    self.q.mark_running(job_id, now_epoch=now)
                elif job["status"] == "running" and now is not None:
                    self.q.touch_lease(job_id, now)
                return {"ok": True, "message": "running"}
            if status == "done":
                if job["status"] == "leased":        # a worker may skip the running ping on a fast job
                    self.q.mark_running(job_id, now_epoch=now)
                self.q.complete(job_id, result_manifest)
                return {"ok": True, "message": "done"}
            if status == "failed":
                _, requeued = self.q.fail(job_id, error or "worker-reported failure")
                return {"ok": True, "message": "requeued" if requeued else "failed"}
            return {"ok": False, "message": "unknown status %r" % status}

    def heartbeat(self, worker_id, job_id, now):
        """Keep a long job's lease alive + the worker's liveness fresh."""
        with self._lock:
            w = self._workers.get(worker_id)
            if w is not None:
                w["last_seen"] = float(now)
            if job_id:
                job = self.q.get(job_id)
                if job is not None and job.get("worker_id") == worker_id:
                    self.q.touch_lease(job_id, now)
            return {"ok": True, "message": "ack"}

    def status(self):
        """Snapshot for the submit/status CLI: queue counts + known workers (with staleness left to the
        caller's clock). Read-only."""
        with self._lock:
            return {"counts": self.q.counts(),
                    "workers": {wid: {"caps": w["caps"], "last_seen": w["last_seen"],
                                      "hostname": w["hostname"]}
                                for wid, w in self._workers.items()}}


# --- gRPC server (lazy import; never touched by the self-test) ------------------------------------
def serve(core, port=50077, block=True):  # pragma: no cover -- VERIFY ON CLUSTER (needs grpc + stubs)
    """Start the gRPC server translating cluster.proto onto `core`. Imports grpc + the generated
    cluster_pb2/_pb2_grpc lazily so importing this module never requires grpc. Regenerate the stubs on
    a worker first (see cluster/proto/cluster.proto header)."""
    import time
    import json
    from concurrent import futures
    try:
        import grpc
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "proto"))
        import cluster_pb2 as pb
        import cluster_pb2_grpc as pbg
    except ImportError as e:
        raise SystemExit("scheduler serve needs grpcio + generated stubs (%s). Regenerate per "
                         "cluster/proto/cluster.proto, then retry. (VERIFY ON CLUSTER)" % e)

    class Servicer(pbg.ClusterServicer):
        def Register(self, req, ctx):
            caps = {"backends": list(req.backends), "vram_gb": req.vram_gb, "ram_gb": req.ram_gb,
                    "images": list(req.images)}
            t = core.register(req.worker_id, caps, time.time(), hostname=req.hostname)
            return pb.RegisterAck(ok=True, message="registered", lease_timeout_s=t)

        def LeaseJob(self, req, ctx):
            caps = {"backends": list(req.caps.backends), "vram_gb": req.caps.vram_gb,
                    "ram_gb": req.caps.ram_gb, "images": list(req.caps.images)}
            job = core.lease_job(req.worker_id, caps, time.time())
            if job is None:
                return pb.LeaseReply(have_job=False, job_json="")
            return pb.LeaseReply(have_job=True, job_json=jobspec.dumps(job))

        def ReportResult(self, req, ctx):
            rm = json.loads(req.result_manifest_json) if req.result_manifest_json else None
            r = core.report_result(req.worker_id, req.job_id, req.status, rm, req.error, time.time())
            return pb.Ack(ok=r["ok"], message=r["message"])

        def Heartbeat(self, req, ctx):
            r = core.heartbeat(req.worker_id, req.job_id, time.time())
            return pb.Ack(ok=r["ok"], message=r["message"])

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    pbg.add_ClusterServicer_to_server(Servicer(), server)
    server.add_insecure_port("[::]:%d" % port)      # LAN-only; sshd/key-auth is the trust boundary
    server.start()
    print("SCHEDULER serving on :%d (queue=%s)" % (port, core.q.root))
    if block:
        server.wait_for_termination()
    return server


def _selftest():
    import shutil
    import tempfile
    root = tempfile.mkdtemp(prefix="k1sched-")
    try:
        q = JobQueue(root, max_attempts=2)
        core = SchedulerCore(q, lease_timeout_s=30)

        cpu = {"backends": ["cpu"], "vram_gb": 0.0, "ram_gb": 64.0}
        cuda = {"backends": ["cpu", "cuda"], "vram_gb": 10.0, "ram_gb": 64.0}
        q.enqueue(jobspec.new_job("20260710T120000-aaa", "recon", "k1recon", ["run-A"],
                                  requires={"backend": "cpu"}, priority=100))
        q.enqueue(jobspec.new_job("20260710T120001-bbb", "train", "k1train", ["ds"],
                                  requires={"backend": "cuda", "min_vram_gb": 8}, priority=100))

        assert core.register("w-cpu", cpu, now=1000.0) == 30
        # cpu worker gets ONLY the cpu job; the cuda job stays queued for a cuda worker.
        j = core.lease_job("w-cpu", cpu, now=1001.0)
        assert j is not None and j["job_id"] == "20260710T120000-aaa"
        assert core.lease_job("w-cpu", cpu, now=1002.0) is None       # no more cpu work
        # a stale worker reporting on a job it doesn't hold is refused, not applied.
        r = core.report_result("someone-else", j["job_id"], "done", now=1003.0)
        assert r["ok"] is False and "held by" in r["message"]
        # the real holder: running -> heartbeat -> done
        assert core.report_result("w-cpu", j["job_id"], "running", now=1004.0)["ok"] is True
        assert q.get(j["job_id"])["status"] == "running"
        core.heartbeat("w-cpu", j["job_id"], now=1005.0)
        assert q.get(j["job_id"])["lease_epoch"] == 1005.0
        assert core.report_result("w-cpu", j["job_id"], "done",
                                  result_manifest={"status": "ok"}, now=1006.0)["ok"] is True
        assert q.get(j["job_id"])["status"] == "done"

        # cuda worker registers + leases the cuda job; then goes silent -> stale reap requeues it.
        core.register("w-cuda", cuda, now=1010.0)
        jt = core.lease_job("w-cuda", cuda, now=1011.0)
        assert jt["job_id"] == "20260710T120001-bbb" and q.get(jt["job_id"])["status"] == "leased"
        # next lease from anyone at now >> lease_timeout reaps the silent cuda lease back to queued.
        core.lease_job("w-cpu", cpu, now=1011.0 + 999.0)              # triggers requeue_stale
        assert q.get(jt["job_id"])["status"] == "queued", q.get(jt["job_id"])["status"]

        # failure path on a FRESH job (the reaped 'bbb' already spent an attempt): fail -> requeue
        # (attempt 1<2), fail again -> terminal failed.
        core.register("w-cuda", cuda, now=2000.0)
        q.enqueue(jobspec.new_job("20260710T120002-ccc", "train", "k1train", ["ds"],
                                  requires={"backend": "cuda", "min_vram_gb": 8}, priority=10))
        jf = core.lease_job("w-cuda", cuda, now=2001.0)       # priority 10 beats the requeued 'bbb'
        assert jf["job_id"] == "20260710T120002-ccc" and jf["attempts"] == 1
        assert core.report_result("w-cuda", jf["job_id"], "failed", error="oom", now=2002.0)[
            "message"] == "requeued"
        jf2 = core.lease_job("w-cuda", cuda, now=2003.0)
        assert jf2["job_id"] == jf["job_id"] and jf2["attempts"] == 2
        assert core.report_result("w-cuda", jf2["job_id"], "failed", error="oom", now=2004.0)[
            "message"] == "failed"
        assert q.get(jf["job_id"])["status"] == "failed"

        st = core.status()
        assert st["counts"]["done"] == 1 and st["counts"]["failed"] == 1
        assert set(st["workers"]) == {"w-cpu", "w-cuda"}
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "selftest":
        raise SystemExit("usage: python cluster/scheduler.py selftest   (serve() is VERIFY ON CLUSTER)")
    _selftest()
    print("SCHEDULER-SELFTEST-OK")
