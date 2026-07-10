# CLUSTER_PLAN.md — LAN heterogeneous job-pool (supersedes the single-desktop P6G)

**Status:** design, 2026-07-10. **Supersedes** the single central-desktop framing of `PHASE_6-8_PLAN.md`
Phase 6G and its §2/§6 lines that assumed one disposable desktop (see "Plan overrides" below). Decided
with the user: build a **heterogeneous job-pool**, not distributed data-parallel SGD (a tiny ACT
imitating a P-controller trains slower split across a LAN than on the 3080 alone — comms dominate). A
DDP hook is designed-for, not built.

## 1. Thesis

Multiple LAN computers pool compute by each running a **worker** that pulls **independent jobs** from a
single **scheduler** and runs them in **Docker**, matched to the job's hardware needs by capability tag.
The parallelism in this workload is across *jobs*, and most of it is embarrassingly parallel:

- **P8.2 reconstruction = one job per run** (N runs → N independent recon jobs).
- **P7.2 training = seed / hyperparameter sweeps** (independent runs, each a job).
- **P6G.4 splat**, **P7.1 ingest**, **P8.3 align** = discrete jobs.

No Kubernetes, no Slurm, no Ray, no message broker. gRPC for the control plane (register / lease /
report), the existing sha256-verified scp bundle transport for the data plane.

## 2. Invariants (carried from the plan; two lines deliberately overridden)

- **The laptop stays the single source of truth.** `runs/`, the dataset identity, and the scheduler's
  job queue live on the laptop (or a designated always-on coordinator). Workers hold only transient job
  data — wiping any worker loses nothing (regenerable from `runs/` + the pinned image).
- **Workers are stateless and disposable.** Everything a worker produces is pushed back to the store;
  nothing authoritative lives only on a worker.
- **No worker has any route or credentials to the robot.** Blast radius zero, by construction.
- **The robot / Jetson is not part of the pool.** The Jetson still does exactly TRT build + shadow node
  + geofence; it is never a worker.
- **Byte-identical doctrine stays robot-side.** GPU-stochastic job outputs (splats, trained weights) are
  metric-gated with recorded seed + image digest, never hash-gated.
- **YAGNI holds within the pivot:** filesystem + JSON queue before a database; scp before rsync-framework;
  gRPC is the *one* new dependency, justified by the register/lease/report RPC contract (a hand-rolled
  HTTP control plane would be more code, not less). A single scheduler, no HA, no autoscaling.

**Plan overrides (the "audit and remove conflicts" record):**
- `PHASE_6-8_PLAN.md` §2 "the desktop is stateless compute" → **"the worker POOL is stateless compute"**
  (one box becomes many; the disposability property is unchanged and now per-worker).
- `PHASE_6-8_PLAN.md` §6 out-of-scope "Standing job queues, watchers, or cloud training infra — the
  desktop substrate is single-job rsync + SSH by design" → **explicitly revisited.** The pivot's whole
  point is a standing scheduler with a job queue. Still no watchers/daemons on the *robot*; still no
  cloud. The queue is a single-writer filesystem/SQLite store, not a broker.
- P6G.1 (single WSL2 desktop) → **per-GPU-worker-node provisioning** (`docs/WSL2_SUBSTRATE.md` re-scoped:
  it's how you stand up *a* CUDA worker, run once per such node).
- P6G.3 (`recon.py`/`Recon.ps1` direct laptop→one-desktop round-trip) → **the scheduler client**
  (`cluster/submit`); recon becomes one job *type* among several. The `recon_manifest.json` + stage
  contract (`docs/RECON_CONTRACT.md`) survives unchanged — it now describes a recon job's output.

## 3. Components

```
 LAPTOP (source of truth, coordinator host)          WORKERS (any LAN box: 3080 desktop, laptop-NPU, a Mac, ...)
 ─────────────────────────────────────────           ──────────────────────────────────────────────────────────
  runs/  (bundles, datasets — authoritative)           worker agent (registers caps, leases jobs, runs Docker,
  cluster/queue/  (jobs as JSON, filesystem)             pushes artifacts back; holds nothing authoritative)
  scheduler  (gRPC server: register/lease/report,        docker: recon image (P8), train image (P7), ingest
             matches job.requires -> worker caps)         each job = `docker run <image> <entrypoint> <args>`
  submit CLI (enqueue a job)                             CUDA worker | CPU worker | NPU worker (capability-tagged)
```

- **Scheduler** (`cluster/scheduler.py`): one process. Holds the worker registry (in-memory, rebuilt
  from re-registration on restart) + the job queue (on disk). gRPC server. Matching = FIFO-by-priority
  over queued jobs, assigned to the first idle worker whose advertised capabilities satisfy
  `job.requires`. Re-queues a job whose worker heartbeat lapses (lease timeout) up to `max_attempts`.
- **Worker** (`cluster/worker.py`): one per machine. On start, detects + advertises capabilities
  (backends, VRAM, RAM, which images are present). Loop: lease a job → fetch inputs (sha-verified scp
  from the store) → `docker run` → collect artifacts → push back → report. Pull model (worker initiates
  every RPC) so workers need no inbound ports beyond their own Docker.
- **gRPC contract** (`cluster/proto/cluster.proto`): `Register`, `LeaseJob`, `ReportResult`,
  `Heartbeat`. Pull-based; the scheduler never dials a worker.
- **Job spec** (`cluster/queue/<state>/<job_id>.json`): `{job_id, type, image, entrypoint, args[],
  inputs:[run_id...], requires:{backend, min_vram_gb, min_ram_gb}, artifacts_out, priority,
  created_utc, status, worker_id, attempts, image_digest, seed, result_manifest}`. `job_id` = UTC stamp
  + short hash; states = `queued | leased | running | done | failed`.
- **submit** (`cluster/submit.py` + a PowerShell wrapper for the laptop): writes a job JSON into
  `queue/queued/`. A recon submit is `--type recon --image k1recon --inputs <run_id> --requires
  backend=cpu`; a train submit is `--type train --requires backend=cuda min_vram_gb=8`.

## 4. Job types (v1)

| type | image | requires | reuses |
|---|---|---|---|
| `ingest` | k1ingest (or the train image) | cpu | `eval/batch_ingest.py` |
| `recon` | k1recon (P6G.2, CPU-lean v1) | cpu | `docs/RECON_CONTRACT.md` stage CLI |
| `train` | k1train (torch cu126) | cuda, min_vram_gb=8 | `docs/TRAIN_CONTRACT.md` + `eval/checkpoint_contract.py` |
| `splat` | k1recon v2 (CUDA/gsplat) | cuda | P6G.4 (deferred) |

A DDP job type (one model, N workers) is **not** built; the job spec's `requires` + a future
`world_size`/`rendezvous` field is where it would slot in. Documented, not implemented.

## 5. Transport & determinism

- **Data plane = the proven sha256-verified bundle push/pull** (`Pull-Run.ps1` idiom, generalized).
  Inputs: worker fetches `runs/<run_id>/` from the store. Outputs: worker pushes `runs/<run_id>/<type>/`
  back; the store (laptop) stays authoritative. A job never mutates its input bundle.
- **Determinism / provenance:** every job result records `image_digest` (the `recon_version` doctrine,
  now `job_version`), `seed`, worker `node_id`, and a `result_manifest.json` (sha256 of every artifact).
  Re-running a job on any worker with the same image + seed + inputs reproduces to the gate's tolerance.

## 6. Scaffold manifest (this pivot) — laptop-authored, cluster-verified

Everything here is authored on the no-Python laptop and is **presumed broken until its self-test passes
on the cluster** (`VERIFY ON CLUSTER`). Each ships a one-command self-test that needs no network, no
Docker, no GPU — an in-process/local-filesystem exercise of the logic.

| Artifact | What | Self-test |
|---|---|---|
| `cluster/proto/cluster.proto` | gRPC service + messages | `protoc` compiles (desktop) |
| `cluster/jobspec.py` | job spec dataclass + JSON (de)serialize + validate | `python -m cluster.jobspec selftest` (round-trip + validation) |
| `cluster/jobqueue.py` | filesystem job queue (enqueue/lease/complete/fail/requeue), single-writer lock | `python -m cluster.jobqueue selftest` (state-machine + capability match + retry, tmp dir) |
| `cluster/scheduler.py` | gRPC server over the queue + worker registry + lease-timeout requeue | `python cluster/scheduler.py selftest` (in-process fake worker round-trip, no gRPC socket) |
| `cluster/worker.py` | capability detect + lease loop + docker-run + artifact push | `python cluster/worker.py selftest --stub` (echo job, no Docker/GPU) |
| `cluster/submit.py` + `desktop/Submit-Job.ps1` | enqueue CLI (Py + laptop PS) | `python cluster/submit.py selftest` / `-SelfTest` (writes+reads a job JSON) |
| `docs/CLUSTER_SETUP.md` | per-worker provisioning runbook (generalizes WSL2_SUBSTRATE) | N/A (runbook; each step self-gates) |

Deferred to the cluster: the real gRPC network loop, Docker execution, capability autodetect against
real hardware, the CUDA train image + torchrun/DDP hook, splat image v2 — all `VERIFY ON CLUSTER`.

## 7. Build order

1. `cluster/jobspec.py` + `cluster/jobqueue.py` — the core, pure-logic, fully self-testable on the laptop.
2. `cluster/proto/cluster.proto` — freeze the RPC contract.
3. `cluster/scheduler.py` (in-process self-test) + `cluster/worker.py --stub`.
4. `cluster/submit.py` + `desktop/Submit-Job.ps1`.
5. `docs/CLUSTER_SETUP.md` (re-scope WSL2_SUBSTRATE to per-worker) + audit the single-desktop docs.
6. The still-needed pivot-agnostic scaffolds (batch_ingest.py etc.) land in parallel — they are *jobs*
   the pool runs, unchanged by the pivot.

North star unchanged: task success of a policy trained on captured data. The pool is how we run the
jobs that get there faster; it is infrastructure, not the thesis.
