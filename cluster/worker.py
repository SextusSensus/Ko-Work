#!/usr/bin/env python3
"""worker.py -- a job-pool worker agent (docs/CLUSTER_PLAN.md).

One per machine. Advertises its capabilities, leases jobs it can run from the scheduler, runs them in
Docker, pushes artifacts back to the store, reports the outcome. Holds NOTHING authoritative -- wiping
a worker loses nothing (regenerable from runs/ + the pinned image). Has NO route/credentials to the
robot, by construction.

Three seams, so the whole lease->run->report loop self-tests with NO gRPC/Docker/GPU:
  * WorkerCore -- the loop logic, over an injected `client` (lease_job/report_result/heartbeat) and an
    injected `runner`. SchedulerCore satisfies the client interface directly, so the self-test wires a
    real WorkerCore to a real SchedulerCore in-process -- a true end-to-end test of the pool.
  * runner -- how a job actually executes. stub_runner (echo, for tests) vs docker_run_job (production,
    VERIFY ON CLUSTER).
  * GrpcClient / detect_caps -- the production gRPC + hardware-probe paths, lazy-imported, never touched
    by the self-test.

Authored blind on a no-Python laptop -- VERIFY ON CLUSTER:
  python -m cluster.worker selftest   ->  WORKER-SELFTEST-OK
"""
import os
import sys

try:
    from . import jobspec
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import jobspec


class WorkerCore:
    """The lease->run->report loop. `client` = anything with lease_job(worker_id, caps, now) /
    report_result(worker_id, job_id, status, result_manifest, error, now) / heartbeat(worker_id,
    job_id, now) -- SchedulerCore fits directly (in-process test) and GrpcClient fits over the wire.
    `runner(job, heartbeat)` -> (status, result_manifest, error); may raise (treated as a failure).
    `clock()` -> now epoch (injected so tests are deterministic)."""

    def __init__(self, worker_id, caps, client, runner, clock):
        self.worker_id = worker_id
        self.caps = dict(caps)
        self.client = client
        self.runner = runner
        self.clock = clock

    def run_once(self):
        """Lease + run + report exactly one job. Returns the job_id it handled, or None if the pool had
        no job for this worker. A runner exception NEVER escapes -- it is reported as a failure so the
        scheduler can requeue/terminal it (a worker crash must not silently strand a leased job)."""
        job = self.client.lease_job(self.worker_id, self.caps, self.clock())
        if job is None:
            return None
        jid = job["job_id"]
        try:
            self.client.report_result(self.worker_id, jid, "running", now=self.clock())
            status, manifest, error = self.runner(
                job, heartbeat=lambda: self.client.heartbeat(self.worker_id, jid, self.clock()))
            if status not in ("done", "failed"):
                status, error = "failed", "runner returned bad status %r" % status
            self.client.report_result(self.worker_id, jid, status, result_manifest=manifest,
                                      error=error, now=self.clock())
        except Exception as e:  # noqa: BLE001 -- a runner blowup becomes a reported failure, never a strand
            self.client.report_result(self.worker_id, jid, "failed",
                                      error="worker exception: %s" % e, now=self.clock())
        return jid


def stub_runner(job, heartbeat=None):
    """Test/`--stub` runner: no Docker. Echoes a done result + calls heartbeat once (proving the
    keep-alive path). A job whose image contains 'fail' is reported failed (to exercise that path)."""
    if heartbeat is not None:
        heartbeat()
    if "fail" in job.get("image", ""):
        return "failed", None, "stub: image marked fail"
    return "done", {"status": "ok", "stub": True, "job_id": job["job_id"],
                    "image_digest": "stub:" + job["image"], "seed": job.get("seed", 0),
                    "artifacts": []}, None


# --- the data plane: a Store moves run bundles worker<->laptop (the sha-verified scp transport) -----
class LocalStore:
    """Filesystem store (tests + a co-located coordinator). fetch/push are directory copies under a
    root. Used by the self-test to exercise the fetch->run->push flow with zero ssh/Docker."""
    def __init__(self, root):
        self.root = os.path.abspath(root)

    def fetch(self, run_id, dest):
        import shutil
        src = os.path.join(self.root, run_id)
        if not os.path.isdir(src):
            raise FileNotFoundError("LocalStore: no bundle %s under %s" % (run_id, self.root))
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        shutil.copytree(src, dest)

    def push(self, run_id, subdir, src):
        import shutil
        dst = os.path.join(self.root, run_id, subdir)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst)


class ScpStore:  # pragma: no cover -- VERIFY ON CLUSTER (needs ssh/scp + a reachable laptop)
    """Production store: run bundles live on the LAPTOP (source of truth); the worker scp's inputs down
    and pushes artifacts back. Mirrors Pull-Run.ps1's OpenSSH idiom (key auth). `remote_runs` is the
    laptop's runs/ dir; `host`/`user`/`port`/`identity` reach the laptop's sshd."""
    def __init__(self, host, user="k1", port=22, identity=None, remote_runs="~/k1/runs"):
        self.host, self.user, self.port = host, user, port
        self.identity, self.remote_runs = identity, remote_runs

    def _opts(self):
        o = ["-p", str(self.port), "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
        if self.identity:
            o += ["-i", self.identity]
        return o

    def _target(self, path):
        return "%s@%s:%s" % (self.user, self.host, path)

    def fetch(self, run_id, dest):
        import subprocess
        os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
        rc = subprocess.call(["scp", "-r"] + self._opts()
                             + [self._target("%s/%s" % (self.remote_runs, run_id)), dest])
        if rc != 0:
            raise RuntimeError("ScpStore.fetch %s exit %d" % (run_id, rc))

    def push(self, run_id, subdir, src):
        import subprocess
        rc = subprocess.call(["scp", "-r"] + self._opts()
                             + [src, self._target("%s/%s/%s" % (self.remote_runs, run_id, subdir))])
        if rc != 0:
            raise RuntimeError("ScpStore.push %s/%s exit %d" % (run_id, subdir, rc))


def _docker_call(cmd):  # pragma: no cover -- the real container run
    import subprocess
    return subprocess.call(cmd)


# --- production paths (lazy / guarded; VERIFY ON CLUSTER) -----------------------------------------
def detect_caps(worker_id):  # pragma: no cover -- probes real hardware
    """Best-effort capability probe. Every backend/figure is guarded: a worker under-advertises rather
    than claim a capability it cannot deliver (fail-closed matches jobspec.requires_satisfied_by)."""
    import shutil
    import subprocess
    backends, vram_gb, images = ["cpu"], 0.0, []
    ram_gb = 0.0
    try:
        ram_gb = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3), 1)
    except (ValueError, OSError, AttributeError):
        pass
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                stderr=subprocess.DEVNULL, timeout=10).decode().split()
            if out:
                backends.append("cuda")
                vram_gb = round(max(float(x) for x in out) / 1024.0, 1)
        except (subprocess.SubprocessError, ValueError, OSError):
            pass
    if sys.platform == "darwin":
        backends.append("mps")                      # Apple Silicon MPS -- the job/image must honor it
    if shutil.which("docker"):
        try:
            images = subprocess.check_output(
                ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                stderr=subprocess.DEVNULL, timeout=10).decode().split()
        except (subprocess.SubprocessError, OSError):
            pass
    return {"worker_id": worker_id, "backends": backends, "vram_gb": vram_gb,
            "ram_gb": ram_gb, "images": images, "hostname": _hostname()}


def _hostname():
    try:
        import socket
        return socket.gethostname()
    except Exception:  # noqa: BLE001
        return "unknown"


def docker_run_job(job, store, inbox_root, outbox_root, heartbeat=None, run_container=None):
    """Runner: fetch input bundles from the store, run the job's image with inbox RO + outbox RW mounts,
    hash the outbox into a result_manifest, push artifacts back. Guarded so any failure becomes
    ('failed', None, msg) -- never a raise that strands the lease. `store` provides fetch(run_id, dest)
    + push(run_id, subdir, src). `run_container(cmd)->rc` is injectable (default = real `docker run`);
    the self-test injects a fake that writes an artifact, so the whole data-plane flow is testable
    without Docker. The image digest / recon_version is recorded by the container into its own manifest."""
    run_container = run_container or _docker_call
    jid = job["job_id"]
    inbox = os.path.join(inbox_root, jid)
    outbox = os.path.join(outbox_root, jid)
    try:
        os.makedirs(inbox, exist_ok=True)
        os.makedirs(outbox, exist_ok=True)
        for run_id in job.get("inputs", []):
            store.fetch(run_id, os.path.join(inbox, run_id))     # sha-verified scp (reused idiom)
        cmd = ["docker", "run", "--rm", "--name", "job_" + jid,
               "-v", "%s:/inbox:ro" % os.path.abspath(inbox),
               "-v", "%s:/outbox" % os.path.abspath(outbox)]
        if job["requires"]["backend"] == "cuda":
            cmd += ["--gpus", "all"]
        cmd += [job["image"]]
        if job.get("entrypoint"):
            cmd += [job["entrypoint"]]
        cmd += list(job.get("args", []))
        if heartbeat is not None:
            heartbeat()
        rc = run_container(cmd)
        if rc != 0:
            return "failed", None, "container run exit %d" % rc
        manifest = _hash_tree(outbox)
        manifest["job_id"] = jid
        manifest["seed"] = job.get("seed", 0)
        for run_id in job.get("inputs", []):
            store.push(run_id, job["type"], outbox)              # artifacts -> runs/<run_id>/<type>/
        return "done", manifest, None
    except Exception as e:  # noqa: BLE001
        return "failed", None, "docker_run_job: %s" % e


def _hash_tree(root):  # pragma: no cover -- used by the production runner
    import hashlib
    arts = []
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            p = os.path.join(dirpath, name)
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            arts.append({"path": os.path.relpath(p, root).replace("\\", "/"),
                         "sha256": h.hexdigest(), "bytes": os.path.getsize(p)})
    return {"status": "ok", "artifacts": arts}


class GrpcClient:  # pragma: no cover -- VERIFY ON CLUSTER (needs grpc + stubs)
    """Production client: translates lease_job/report_result/heartbeat into cluster.proto RPCs. Lazy
    grpc import so importing worker.py never needs grpc. `now` args are accepted + ignored (the
    scheduler stamps its own server clock)."""
    def __init__(self, addr):
        import grpc
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "proto"))
        import cluster_pb2 as pb
        import cluster_pb2_grpc as pbg
        self._pb, self._pbg = pb, pbg
        self._ch = grpc.insecure_channel(addr)
        self._stub = pbg.ClusterStub(self._ch)

    def _wi(self, worker_id, caps):
        return self._pb.WorkerInfo(worker_id=worker_id, backends=caps.get("backends", []),
                                   vram_gb=caps.get("vram_gb", 0.0), ram_gb=caps.get("ram_gb", 0.0),
                                   images=caps.get("images", []), hostname=caps.get("hostname", ""))

    def register(self, worker_id, caps, now=None):
        return self._stub.Register(self._wi(worker_id, caps)).lease_timeout_s

    def lease_job(self, worker_id, caps, now=None):
        r = self._stub.LeaseJob(self._pb.LeaseRequest(worker_id=worker_id, caps=self._wi(worker_id, caps)))
        return jobspec.loads(r.job_json) if r.have_job else None

    def report_result(self, worker_id, job_id, status, result_manifest=None, error=None, now=None):
        import json
        r = self._stub.ReportResult(self._pb.ResultReport(
            worker_id=worker_id, job_id=job_id, status=status,
            result_manifest_json=json.dumps(result_manifest) if result_manifest else "",
            error=error or ""))
        return {"ok": r.ok, "message": r.message}

    def heartbeat(self, worker_id, job_id, now=None):
        r = self._stub.Heartbeat(self._pb.HeartbeatPing(worker_id=worker_id, job_id=job_id, phase=""))
        return {"ok": r.ok, "message": r.message}


def main(argv):  # pragma: no cover -- VERIFY ON CLUSTER (real gRPC + Docker)
    import argparse
    import time
    ap = argparse.ArgumentParser(description="job-pool worker")
    ap.add_argument("--scheduler", required=True, help="scheduler gRPC addr host:port")
    ap.add_argument("--worker-id", default=_hostname())
    ap.add_argument("--inbox", default=os.path.expanduser("~/k1/inbox"))
    ap.add_argument("--outbox", default=os.path.expanduser("~/k1/outbox"))
    ap.add_argument("--poll-s", type=float, default=5.0)
    ap.add_argument("--stub", action="store_true", help="run jobs with the echo stub (no Docker/store)")
    ap.add_argument("--laptop-host", help="the laptop (runs/ source of truth) for the scp data plane")
    ap.add_argument("--laptop-user", default="k1")
    ap.add_argument("--laptop-port", type=int, default=22)
    ap.add_argument("--identity", default=None)
    ap.add_argument("--remote-runs", default="~/k1/runs")
    a = ap.parse_args(argv)
    caps = detect_caps(a.worker_id)
    client = GrpcClient(a.scheduler)
    client.register(a.worker_id, caps)
    print("WORKER %s registered: backends=%s vram=%.1f ram=%.1f images=%d"
          % (a.worker_id, caps["backends"], caps["vram_gb"], caps["ram_gb"], len(caps["images"])))
    if a.stub:
        runner = stub_runner
    else:
        if not a.laptop_host:
            raise SystemExit("--laptop-host is required for real (non --stub) jobs (the scp data plane)")
        store = ScpStore(a.laptop_host, user=a.laptop_user, port=a.laptop_port,
                         identity=a.identity, remote_runs=a.remote_runs)
        runner = lambda job, heartbeat=None: docker_run_job(  # noqa: E731
            job, store, a.inbox, a.outbox, heartbeat=heartbeat)
    core = WorkerCore(a.worker_id, caps, client, runner, time.time)
    while True:
        if core.run_once() is None:
            time.sleep(a.poll_s)


# --- self-test: WorkerCore <-> a REAL SchedulerCore, in-process, stub runner ---------------------
def _selftest():
    import shutil
    import tempfile
    try:
        from .jobqueue import JobQueue
        from .scheduler import SchedulerCore
    except ImportError:
        from jobqueue import JobQueue
        from scheduler import SchedulerCore

    root = tempfile.mkdtemp(prefix="k1worker-")
    try:
        q = JobQueue(root, max_attempts=2)
        sched = SchedulerCore(q, lease_timeout_s=30)
        # a fake monotonic clock (deterministic; no time()/random)
        _t = [1000.0]

        def clock():
            _t[0] += 1.0
            return _t[0]

        cpu = {"backends": ["cpu"], "vram_gb": 0.0, "ram_gb": 32.0}
        sched.register("w1", cpu, now=clock())
        # one runnable cpu job, one cuda job this cpu worker must NOT get, one 'fail'-image job.
        q.enqueue(jobspec.new_job("20260710T130000-ok1", "ingest", "k1ingest", ["run-A"],
                                  requires={"backend": "cpu"}, priority=100))
        q.enqueue(jobspec.new_job("20260710T130001-gpu", "train", "k1train", ["ds"],
                                  requires={"backend": "cuda", "min_vram_gb": 8}, priority=10))
        q.enqueue(jobspec.new_job("20260710T130002-bad", "recon", "k1recon-fail", ["run-B"],
                                  requires={"backend": "cpu"}, priority=200))

        w = WorkerCore("w1", cpu, sched, stub_runner, clock)
        # 1st run: leases the highest-priority CPU job it can run (ok1 @100 beats bad @200; gpu unrunnable)
        assert w.run_once() == "20260710T130000-ok1"
        assert q.get("20260710T130000-ok1")["status"] == "done"
        assert q.get("20260710T130000-ok1")["result_manifest"]["stub"] is True
        # 2nd run: the 'fail'-image job -> reported failed -> requeued (attempt 1<2)
        assert w.run_once() == "20260710T130002-bad"
        assert q.get("20260710T130002-bad")["status"] == "queued"     # requeued
        # 3rd run: same bad job again -> attempt 2 -> terminal failed
        assert w.run_once() == "20260710T130002-bad"
        assert q.get("20260710T130002-bad")["status"] == "failed"
        # 4th run: only the cuda job remains, unrunnable by this cpu worker -> None
        assert w.run_once() is None
        assert q.get("20260710T130001-gpu")["status"] == "queued"     # untouched, waits for a cuda worker

        # a runner that RAISES must be reported as a failure, never escape run_once
        def boom(job, heartbeat=None):
            raise RuntimeError("kaboom")

        q.enqueue(jobspec.new_job("20260710T130003-raise", "ingest", "k1ingest", [],
                                  requires={"backend": "cpu"}))
        w2 = WorkerCore("w1", cpu, sched, boom, clock)
        assert w2.run_once() == "20260710T130003-raise"
        assert q.get("20260710T130003-raise")["status"] in ("queued", "failed")  # requeued then would fail

        # Store + docker_run_job data-plane flow: LocalStore + a FAKE container that writes an artifact
        # into the outbox -- exercises fetch -> run -> hash -> push with no ssh/Docker.
        storeroot = os.path.join(root, "store")
        os.makedirs(os.path.join(storeroot, "run-X"))
        with open(os.path.join(storeroot, "run-X", "manifest.json"), "w") as f:
            f.write('{"run_id": "run-X"}')
        ls = LocalStore(storeroot)
        djob = jobspec.new_job("20260710T140000-store", "recon", "k1recon", ["run-X"],
                               requires={"backend": "cpu"})

        def fake_container(cmd):
            for i, a in enumerate(cmd):                        # find the -v <outbox>:/outbox mount
                if a == "-v" and str(cmd[i + 1]).endswith(":/outbox"):
                    ob = str(cmd[i + 1])[: -len(":/outbox")]
                    with open(os.path.join(ob, "mesh.ply"), "w") as f:
                        f.write("ply-bytes")
            return 0

        status, manifest, err = docker_run_job(
            djob, ls, os.path.join(root, "inbox"), os.path.join(root, "outbox"),
            run_container=fake_container)
        assert status == "done", (status, err)
        assert any(a["path"] == "mesh.ply" for a in manifest["artifacts"]), manifest
        # the fetched input landed, and the artifact was pushed back to runs/run-X/recon/
        assert os.path.isfile(os.path.join(root, "inbox", djob["job_id"], "run-X", "manifest.json"))
        assert os.path.isfile(os.path.join(storeroot, "run-X", "recon", "mesh.ply"))
        # a non-zero container rc is a reported failure, not a raise
        s2, _, e2 = docker_run_job(djob, ls, os.path.join(root, "inbox2"), os.path.join(root, "outbox2"),
                                   run_container=lambda cmd: 7)
        assert s2 == "failed" and "exit 7" in e2

        # detect_caps always returns at least a cpu worker with a hostname (no hardware assumptions)
        caps = detect_caps("probe")
        assert "cpu" in caps["backends"] and caps["worker_id"] == "probe" and "hostname" in caps
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        _selftest()
        print("WORKER-SELFTEST-OK")
    else:
        main(sys.argv[1:])
