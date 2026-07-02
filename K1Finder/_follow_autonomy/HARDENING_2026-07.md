# Follow-stack hardening — current state (2026-07-02)

Single source of truth for what shipped in the June-26 → July-02 hardening arc
(relock-fall fixes → OSNet audit → P0 observability → P1 field-ops → final adversarial
review, 23/23 findings fixed). Where an older design doc disagrees with this file,
**this file wins**. Validation gate before arming: `ARM_VALIDATION_RUNBOOK.md`.

## What shipped (by theme)

**Anti-lunge / relock (the June-26 fall class)** — velocity slew limiter applied before the
sacred clamps at every command site, with a `forbid_forward` re-zero AFTER the slew (a ramp
from a high baseline can never re-leak a forbidden forward). Armed relock admission gate:
absolute range cap, aged-median range-jump test, require-validated-depth, N-frame same-id
streak, post-relock no-forward one-shot. Iso-bypass lets a strong frozen-anchor match relock
in a crowd; passive vote runs the full REACQUIRE stand.

**Perception truthfulness (the CRITICAL final-review find)** — CamNode is serviced by a
dedicated background executor thread (`cam-spin`), so fps EMAs and frame stamps measure the
**sensor**, not the control loop (the old one-`spin_once`-per-tick pattern capped measured
depth at ~3-5 fps on a healthy 13 fps camera and false-latched the floor). Depth-fps floor →
TURN-ONLY (latches on rate **or** age, needs a frame pair); `_range_for` requires a min
valid-pixel fraction + dispersion ceiling + RGB↔depth skew bound before a reading may
authorize forward drive; DRIVE precondition requires RGB+depth fresh (rate **and** age)
before kWalking, else exits 4 (`DRIVE-ABORT`) — never walks blind.

**OSNet health observability** — REID engine health is surfaced end-to-end: init EP
assertion (CPU-EP ⇒ `REID-DEGRADED`, armed relock refused), mid-run all-None watchdog
(latches ⇒ audit-only; de-latches from TRACK **or** the REACQUIRE vote), app badge
(`OSNet(TRT)`/`CUDA` green, `CPU-EP` amber, `HIST`/`DEGRADED` red), whitelisted + colored
node telemetry in the Tracker log, ONNX auto-staged with size verification.

**Ops / diagnosis** — follow exit codes classified (0 stop / 3 bridge-compile-failed →
tails `k1_compile.err` / 4 drive-refused / other crash → tails `k1_follow.err`); NO-FRAME +
RELOC lines throttled ~1/s; `striped` appearance removed from the operator combo (documented
lock-loser; dev CLI path intact).

## Flag reference (new/changed; every one has a 0/off byte-identical path)

| Flag | Default | Purpose |
|---|---|---|
| `--vx-slew` / `--vyaw-slew` | 0.06 / 0.10 | per-tick accel limit, before the hard clamps |
| `--min-safe-range` | **0.6** (was 0 = off) | depth-validated close-range forward floor + debounced geofence stand |
| `--reloc-range-streak` / `--reloc-max-range-jump-m` / `--reloc-max-range-m` | 2 / 2.5 / 5.0 | armed-relock range admission |
| `--reloc-range-stale-s` | 10 | ages out the range reference so a far-walked target can relock |
| `--reloc-iso-bypass-anchor` | per-backend (osnet 0.55) | crowd relock on a strong frozen-anchor match |
| `--reid-fault-k` | 5 | all-None embed frames before `REID-DEGRADED` (auto-disarm) |
| `--min-depth-fps` / `--depth-starved-margin` | 5.0 / 1.0 | depth floor → TURN-ONLY (rate or age-DOWN) |
| `--depth-min-valid-frac` / `--depth-max-dispersion-m` / `--depth-max-skew-ms` | 0.30 / 0.5 / 250 | stricter `depth` label in `_range_for` |
| `--drive-min-fps` / `--drive-ready-secs` | 8.0 / 2.0 | DRIVE precondition (depth half skipped when `--depth-topic none`) |

## Node→app log contract (column-0 prefixes; whitelist regex in `K1Finder.ps1`)

`REID-ENGINE ok/FAILED` · `REID-DEGRADED (cpu-ep*|all-none|recovered)` · `DEPTH <state> fps=` ·
`DEPTH-STARVED (+cleared)` · `RGB fps= (all-topics — combined intake of BOTH RGB topics, ~2×
per-camera; liveness number, depth side is exact)` · `RELOC-*` · `NO-FRAME stall=` ·
`DRIVE-WAIT/READY/ABORT` · `ARM-REFUSED`. Colors: green = recovery/READY, amber =
HOLD/STALE/ABORT/REFUSED/cpu-ep, red = fault family.

## Validation status

- On-robot (2026-07-02, preview): OSNet loads via **TensorRT** on the Orin; depth heartbeat
  reads true publish rate (11–13 fps); **zero** spurious `DEPTH-STARVED`; no thread races.
- NOT yet validated: everything in `ARM_VALIDATION_RUNBOOK.md` Phases 1–2 (drive-mode
  mechanisms + armed relock). `Arm re-lock` stays OFF until that passes.

## Known-stale claims in older docs

- `STAGE4_OSNET.md` "batched embed + every-n cache" — the code path is **live** now
  (`is not`→`!=` fixed) but the shipped ONNX is **fixed batch=1**, so `embed_batch` still
  runs per-row; the every-n cache defaults off (`--reid-every-n 1`). True batching needs the
  dynamic-axis re-export (P2 #13).
- `OPS_BEHAVIOR_EVAL.md` "bridge command-staleness deadman MISSING" — **wrong**; it exists
  and is the most mature safety code in the repo (50 Hz watchdog, zero→kPrepare, heartbeat
  deadman). Its replay-eval gate is still spec-only (P2 #14).
- `PLAN.md` / `STAGE_2_3_IMPL.md` line anchors predate the gesture/lock-trigger subsystem —
  re-grep, don't trust offsets.
