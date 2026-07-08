# Run Artifacts — what a K1 follow run records (P6.1 audit)

**Scope:** the inventory of what the always-on and opt-in recorders actually carry, so the P7
(obs→action policy) and P8 (RGBD reconstruction) pipelines are not built blind. This is the audit
called for by **P6.1** plus the one recorder extension it required (camera intrinsics). Recording
only — **no follow-behavior change** (the intrinsics emit is gated on the default-off Rerun sink, so
a non-capture run is byte-identical).

---

## 1. Two recorders, two lifetimes

| Artifact | Trigger | Lifetime | Carries |
|---|---|---|---|
| `k1_events.jsonl` | **always on** (`--event-log`, default `/home/booster/k1_events.jsonl`) | every run | per-tick safety/decision signals (JSON, line-buffered) |
| `k1_follow_<epoch>.rrd` | **opt-in** (`--rerun`, default **off**) | capture runs only | RGB, depth, scalars, boxes, FSM text, **intrinsics** |
| `intrinsics.json` | with the `.rrd` (P6.1) | capture runs only | derived pinhole camera model (sidecar beside the `.rrd`) |
| `k1_follow.err` | always (stderr redirect in `run_*.sh`) | every run | text log incl. `LOOP-MS`, `DEPTH`, `RGB`, `DRIVE-*` lines |

**Load-bearing finding:** pixels and depth are **not always-on**. They exist only when `--rerun` is
passed, and the `demo`/`field` profiles inherit `rerun: false` (kept off in the headless drive path
for loop-cost — P4.5). The always-on JSONL has the *decision* signals but no *perception* frames.

**Consequence for P6/P7/P8:** a run is only P7/P8-usable if it was recorded with `--rerun` on (save
mode). The offload substrate (P6.2) should bundle whatever `.rrd` + `intrinsics.json` exist and mark a
run's capture-completeness; a policy/reconstruction run must select a capture-enabled profile. See the
open item in `DECISIONS.md` (a `capture.yaml` profile that turns `rerun` on with intrinsics + a modest
`rerun_image_every_n`, distinct from `demo`/`field`).

---

## 2. `k1_events.jsonl` — per-tick JSONL (P5.3, always on)

Written by `common.EventLog` from the control loop (`follow_person_k1.py`, one `tick()` per iteration,
~10/s). Line-buffered so the last ticks survive a hard crash; never raises into the loop.

| field | meaning | clock |
|---|---|---|
| `t` | wall time, `round(time.time(), 3)` | **wall (`time.time()`)** |
| `fsm` | FSM state string (`S_TRACK`…) | — |
| `walk` | bridge is in the walking phase (bool) | — |
| `vx`, `vy`, `vyaw` | **command actually sent** to the bridge this tick | — |
| `loop_ms` | control-loop work time this tick | monotonic Δ |
| `seed` | a target is seeded (bool) | — |
| `hb_req` | heartbeat is required this run (bool) | — |

**Gap vs the plan's ideal `(cmd_vel_sent, fsm_state, hb_age, staleness_event)`:** `cmd_vel_sent` and
`fsm_state` are present (`vx/vy/vyaw`, `fsm`); `hb_age` and `staleness_event` are **not** yet fields —
the JSONL logs `hb_req` (policy) but not the live heartbeat age, and staleness lives only in the C++
bridge's stderr, not this JSONL. Not required by the P6.1 gate; flagged for P7.4, which extends this
same line with the shadow-action columns and is the natural place to add `hb_age`.

---

## 3. `k1_follow_<epoch>.rrd` — Rerun recording (opt-in, capture runs)

Written by the crash-safe `RerunSink` (`k1_rerun.py`); default-off and fully inert. Dual timeline:
`frame_idx` (sequence, the camera `_seq`) and `wall` (`time.time()`).

### 3.1 Perception streams

| entity | source thread | rate | clock / alignment |
|---|---|---|---|
| `/camera/rgb` (`Image`) | cam-spin `_cb` | camera rate **÷ `rerun_image_every_n`** (default 3 → ~4 Hz on a ~13 Hz cam) | `wall`=`time.time()` at callback; `frame_idx`=`_seq` |
| `/camera/depth` (`DepthImage`, metres) | cam-spin `_depth_cb` | depth rate ÷ `rerun_image_every_n` | `wall`=`time.time()`; `frame_idx`= **current RGB `_seq`** (best-effort, *not* stamp-matched) |
| `/camera/rgb` (`Pinhole`) **NEW (P6.1)** | control loop, first frame | once/run, `static` | — |

**Depth↔RGB alignment is loose.** Depth is keyed to whatever RGB `_seq` is current when the depth
callback fires, not to a matched capture timestamp. For offline RGBD (P8) this is close but not
frame-accurate; both carry their own `wall` stamp, so downstream should join **by wall time**, not by
assuming a shared `frame_idx`. Both RGB and depth are decimated by the *same* `rerun_image_every_n`,
so raising capture fidelity for P8 means lowering that N (a capture-profile knob), at loop-cost.

### 3.2 Decision / health scalars (per tick, cheap)

`/follow/range`, `/follow/range_source` (2=depth 1=bboxH 0=none), `/follow/bearing`, `/cmd/vx`,
`/cmd/vyaw`, `/cmd/forbid_forward`, `/reid/sim`, `/track/conf`, `/track/cost`,
`/camera/rgb/target` (`Boxes2D`), `/fsm/state`(+`_id`), `/diag/loop_ms`, `/diag/rr_track_ms`,
`/health/{depth_fps,rgb_fps,depth_starved}`, `/reflex/{clearance_m,vx_cap}`, and the static
`/follow/range/{min_safe,standoff,max}` reference lines.

### 3.3 Camera intrinsics (added by P6.1)

- **In the `.rrd`:** `rr.Pinhole` on `/camera/rgb` (static), so the recording is self-describing.
- **Sidecar:** `intrinsics.json` beside the `.rrd`, machine-readable without opening the `.rrd`
  (what P6.2 offloads and P8.1 calibration consumes).

```json
{ "model": "pinhole_from_hfov", "approximate": true,
  "width": 1280, "height": 720, "hfov_deg": 90.0,
  "fx": 640.0, "fy": 640.0, "cx": 640.0, "cy": 360.0,
  "note": "fx from --hfov-deg; fy:=fx (no vertical FOV published); calibrate in P8.1" }
```

**This rig publishes no `CameraInfo`.** The follow stack already derives focal length from
`--hfov-deg` (`perception.focal_px`) for its bearing/range pinhole; P6.1 records that same derived
model rather than inventing a new source. It is **`approximate: true` on purpose** — good enough to
document the run, *not* good enough for metric reconstruction to trust. **P8.1 must replace it** with a
factory/`CameraInfo` intrinsic or a one-time checkerboard calibration before any TSDF/odometry relies
on it. (Values above are illustrative; the real `width/height/hfov_deg` come from the first frame +
the run's `--hfov-deg`.)

---

## 4. Clock source

All three durable artifacts stamp wall time from **`time.time()`** (the JSONL `t`, the `.rrd` `wall`
timeline, the RGB/depth callback stamps). `loop_ms` and internal freshness use `time.monotonic()`
deltas only (never as absolute stamps). **Common join key across artifacts = `time.time()` wall
seconds.** Note wall time is not monotonic (NTP steps); the Jetson is typically offline, so drift is
the practical concern, not steps — good enough to align RGB/depth/JSONL within a run.

---

## 5. Odometry — audited, NOT yet recorded

- The C++ bridge (`loco_follow_bridge.cpp`) is a **velocity-out** stdin channel; it exposes **no**
  odometry/pose feedback. So odometry cannot come from the bridge.
- Per project notes the K1 **does** publish planar odometry on a ROS topic (`/odometer_state`,
  x/y/θ). Recording it is cheap (one more BEST_EFFORT subscription + one scalar triple per tick) and
  would give P8 a trajectory prior and P7 an extra observation channel.
- **Not wired here** because the exact ROS **message type** can't be confirmed off-robot, and adding a
  live subscription with a guessed type is not a mechanical/verifiable change. Deferred to a small
  follow-up commit once confirmed on-robot:
  ```
  ros2 topic info /odometer_state          # -> exact type + publisher
  ros2 topic hz   /odometer_state          # -> rate
  ```
  Tracked in `DECISIONS.md`. It is **not** in the P6.1 gate (which lists RGB + depth + intrinsics +
  per-tick JSONL).

---

## 6. P6.1 gate — verify on a fresh capture run  `VERIFY ON ROBOT`

Off-robot this repo has no Python/YOLO/clips, so the byte-identical replay gate and the artifact
checks below **were not run here** — they are the on-robot acceptance steps.

1. **Byte-identical (recording off):** the intrinsics emit is gated on `rerun_sink._RR.ok`, so a
   non-`--rerun` run must be unchanged. Run the standard `replay_eval selftest` (`SELFTEST-OK`) and the
   decision-stream diff vs the pre-change build — expect empty.
2. **Capture run produces the bundle:** launch with `--rerun` and drive briefly, then confirm:
   - `k1_follow_<epoch>.rrd` exists and, opened in Rerun, shows `/camera/rgb` **and** `/camera/depth`
     images plus the `/camera/rgb` Pinhole.
   - `intrinsics.json` sits **beside** the `.rrd` with sane `width/height/fx` for the run's `--hfov-deg`.
   - `k1_events.jsonl` has one JSON line per tick with a `t` wall stamp.
   - RGB, depth, and JSONL `t` values overlap on the same wall-time window (the common clock).
3. **Rates:** from `k1_follow.err`, confirm depth is actually publishing (`DEPTH FRESH fps=…`) during
   the captured window — a run with dead depth records RGB-only and is not P8-usable.

---

## 7. Offload bundles (P6.2)

A finished run is packaged into a self-contained, verifiable bundle and pulled to this workstation.

**Jetson side — `robot/offload_run.sh`** (post-session; nothing driving). Assembles
`/home/booster/runs/<run_id>/` where `run_id = <UTC stamp>_<deploy-sha|nogit>`, containing the
`.rrd` + `intrinsics.json` (capture runs), `events.jsonl`, `k1_follow.err`, the `config/` in effect,
an optional `DEPLOY_VERSION`, and a `manifest.json` (per-file size + SHA-256, profile, duration,
totals). Assembly is atomic (staged in a hidden `.partial`, `mv`'d into place) and **retry-safe**:
sources are only rotated (the `.rrd` moved out, the append-mode `events.jsonl` truncated to start a
fresh per-run log) **after** a successful publish, so an interrupted offload leaves sources intact.
Then a `--keep N` retention prunes oldest-first. Invoked with the active `--profile` (the launcher
knows it) and never touches the control loop.

**Workstation side — `desktop/Pull-Run.ps1`** (this Windows PC; no rsync/Python needed). Over the same
OpenSSH channel the app uses, it fetches `manifest.json` first, then each file **only if** missing or
its SHA-256 doesn't match — so an interrupted pull, re-run, transfers just the gap and converges
(`PULL-OK` / `PULL-INCOMPLETE`). `-List` shows remote bundles; no `-RunId` pulls the latest.

`run_id` carries a real git SHA only if deploy drops `/home/booster/DEPLOY_VERSION` (a short SHA);
otherwise the manifest records `nogit` (see `DECISIONS.md` P6.2a). Local store defaults to `runs/` at
the repo root; a second GPU box for P7/P8 heavy compute can re-sync from there later.
