# CLUSTER_SETUP.md — stand up the LAN job-pool

Provisioning runbook for `docs/CLUSTER_PLAN.md`. Decisions-first + self-gating: each step says WHY, then
the commands, then a PROVE-IT check. Generalizes `docs/WSL2_SUBSTRATE.md` (which is the CUDA-worker WSL2
half) to the whole pool: coordinator + heterogeneous workers.

## 0. Topology (who runs what)

| Role | Machine | Runs | Holds |
|---|---|---|---|
| **Data source of truth + submit** | laptop (no Python) | `Submit-Job.ps1`, `Pull-Run.ps1` | `runs/` (authoritative) |
| **Coordinator** | a Python node (default: the desktop/WSL) | `cluster/scheduler.py serve`, the `queue/` | transient queue (regenerable) |
| **Worker(s)** | any LAN box (3080/WSL, a Mac, a CPU box) | `cluster/worker.py` + Docker | transient job scratch |

The desktop commonly **co-locates** the coordinator (scheduler) and a CUDA worker — two processes, one
box. The Jetson/robot is **never** in the pool.

> **Step 0 — run the self-tests first (they are LOCALLY VERIFIABLE).** On any Python node, before wiring
> anything, confirm the code the laptop authored actually runs:
> ```
> python -m cluster.jobspec selftest && python -m cluster.jobqueue selftest && \
> python -m cluster.scheduler selftest && python -m cluster.worker selftest && \
> python -m cluster.submit selftest && python eval/batch_ingest.py selftest
> ```
> These were authored on the laptop and **already pass under its anaconda python 3.13.9** (no Docker/
> GPU/gRPC needed) — but re-run them in the coordinator's env to catch a version skew before trusting it.

## 1. SSH key mesh (the trust boundary)

**Decision:** key auth only, no passwords; the LAN + sshd is the whole trust boundary (gRPC is
insecure-channel, LAN-only). Three edges:
- **laptop → coordinator** (Submit-Job.ps1): `ssh-keygen -t ed25519 -f ~/.ssh/id_k1` on the laptop;
  install the pub key in the coordinator's `~/.ssh/authorized_keys`.
- **laptop → workers** (workers pull input bundles / push artifacts to the laptop's `runs/`): the same
  key, or the worker fetches via the laptop's OpenSSH server. Simplest: give each worker read access to
  the laptop `runs/` over scp with a key.
- **coordinator ↔ workers**: workers dial the coordinator's gRPC port (default 50077) — no ssh needed
  for the control plane; only the data plane (bundle scp) uses ssh.

*PROVE IT:* `ssh -i ~/.ssh/id_k1 -p 2222 <user>@<coordinator> 'echo ok'` prints `ok` with no prompt.

## 2. Coordinator (scheduler + queue)

**Decision:** the coordinator is the always-on Python node; the queue is a filesystem dir on its ext4
(never `/mnt/c` — see WSL2_SUBSTRATE). It holds only the transient queue.

```
git clone <repo> ~/k1/Ko-Work && cd ~/k1/Ko-Work        # the checkout Submit-Job.ps1's -RepoDir points at
python -m venv ~/k1/venv && ~/k1/venv/bin/pip install grpcio grpcio-tools    # control-plane deps
python -m grpc_tools.protoc -I cluster/proto --python_out=cluster/proto \
       --grpc_python_out=cluster/proto cluster/proto/cluster.proto           # generate the stubs (gitignored)
python cluster/scheduler.py serve   # (add a serve CLI wrapper, or import serve(SchedulerCore(JobQueue('~/k1/queue'))))
```

*PROVE IT:* the scheduler prints `SCHEDULER serving on :50077`; from the laptop,
`Submit-Job.ps1 -HostName <coordinator> -Type ingest -Image k1ingest -Requires 'backend=cpu'` prints
`SUBMIT-OK` and a `queued/<job_id>.json` appears in `~/k1/queue/queued/`.

## 3. Workers

Each worker runs `cluster/worker.py --scheduler <coordinator>:50077`, advertising the capabilities it
autodetects (`detect_caps`). Provision per hardware class:

- **CUDA worker** (the 3080 desktop): follow `docs/WSL2_SUBSTRATE.md` (WSL2 + Docker-Engine-in-distro +
  nvidia-container-toolkit; driver on Windows only; data on ext4). Then `pip install grpcio`, build the
  recon/train images (P6G.2), and run the worker. It advertises `backends=[cpu,cuda]`.
- **CPU worker** (any Linux/WSL box): Docker Engine + `pip install grpcio`; run the worker. Advertises
  `backends=[cpu]` — it takes recon/ingest jobs, never `backend=cuda` ones (fail-closed match).
- **NPU worker** (the laptop's XDNA2, a Mac's MPS): optional/later. The worker would advertise `npu`/
  `mps`; a job must have an image + entrypoint that actually uses that backend. Not wired in v1 (no job
  type targets npu/mps yet) — the capability tag exists so it slots in without a scheduler change.

*PROVE IT (per worker):* the worker prints `WORKER <id> registered: backends=[...]`; the P6G.1 gate
`ssh ... 'docker run --rm --gpus all nvidia/cuda:<tag> nvidia-smi'` shows the GPU on a CUDA worker.

## 4. End-to-end (the pool works)

1. Laptop pulls a capture run to `runs/` (`Pull-Run.ps1`) — or `Sync-Runs.ps1` it to the coordinator.
2. Laptop `Submit-Job.ps1 -Type recon -Image k1recon:dev -Inputs <run_id> -Requires 'backend=cpu'`.
3. A CPU/CUDA worker leases it, fetches the bundle from the laptop, `docker run`s the recon image,
   pushes `runs/<run_id>/recon/` back to the laptop, reports done.
4. `cluster/worker.py` self-suspends / the scheduler requeues if a worker dies (stale-lease reaping).

*GATE (P6G.3, VERIFY ON CLUSTER):* the STUB round-trip first (`recon_job.sh --stub`), then the real
image; then wipe the coordinator's queue + a worker's scratch and re-submit → tolerance-identical
output lands in the laptop `runs/` (proves workers + coordinator are disposable; the laptop is the only
thing you back up).

## 5. What's still `VERIFY ON CLUSTER` (not verifiable on the laptop)

The self-tests in step 0 verify the LOGIC locally. These need the real substrate: the gRPC network loop
(scheduler `serve()` + `GrpcClient`), Docker execution + `detect_caps` against real hardware, the recon/
train image builds, cross-pyarrow `content_hash` stability for `batch_ingest`, and the P6G.3
wipe-and-resubmit disposability gate.
