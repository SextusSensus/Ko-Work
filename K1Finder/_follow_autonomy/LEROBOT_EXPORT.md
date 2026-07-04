# LeRobot export (Phase 4) — pipeline-proof, honestly scoped

**Tool:** `_follow_autonomy/eval/rrd_to_lerobot.py` — a K1-follow Rerun `.rrd` → a LeRobot-v3-shaped
episode (`observation.state` + `action` + `head_rgb` video).

## What this is (and is NOT)
- **IS:** an **internal QA / labeling / pipeline-plumbing** tool. It proves the `.rrd → LeRobot`
  ingest path works end-to-end, de-risking the ingest for the real capture rig.
- **IS NOT:** thesis training data. The follow episode is **mono head-RGB + (optional) flaky
  head-depth + a 2-DOF twist from a hand-written P-controller**. It is not the dual-POV + LiDAR +
  skeletal capture on *any* axis. **Do not pitch the output as "we capture training data"** — one
  diligence question collapses that (see `RERUN_PLAN.md` thesis-fit). Label any dataset produced
  here *internal tooling*.

## Two honest findings that shaped this
1. **There is NO native `rerun → LeRobot` exporter in rerun 0.33** (checked the package + CLI). The
   plan's assumption that we'd "run Rerun's built-in export" was wrong. So we read the `.rrd`
   ourselves and map it. This is fine — the mapping is the point.
2. **`rr.save()` writes a *legacy footerless* `.rrd`** in this SDK build, so the `dataframe`/`view`
   read API is unavailable. The working read path is
   `rerun.experimental.RrdReader(path).stream().collect()` → iterate chunks → `to_record_batch()`.
   That is what the exporter uses (validated).

## Schema mapping
| LeRobot feature | Source entity in the `.rrd` | Notes |
|---|---|---|
| `observation.state[0]` `range_m` | `/follow/range` | metres |
| `observation.state[1]` `bearing_deg` | `/follow/bearing` | degrees |
| `observation.state[2]` `rsrc_is_depth` | `/follow/range_source` | 2.0 (depth) → 1.0, else 0.0 |
| `observation.state[3]` `anchor_sim` | `/reid/sim` | OSNet anchor cosine sim |
| `observation.state[4]` `conf` | `/track/conf` | detector/assoc confidence |
| `observation.state[5]` `depth_fps` | `/health/depth_fps` | **LIVE `.rrd` only**; 0.0-filled for replay `.rrd`s |
| `observation.state[6]` `fsm_state_id` | `/fsm/state_id` | 3=TRACK 2=REACQUIRE 1.5=SEARCHING 1=SEARCH 0=PARKED |
| `action[0]` `vx` | `/cmd/vx` | forward m/s (truthful post-clamp) |
| `action[1]` `vyaw` | `/cmd/vyaw` | yaw rad/s. `vy ≡ 0` (2-DOF) |
| `observation.images.head_rgb` | `/camera/rgb` | `Image` → mp4 (h264). Depth is READ from live `.rrd`s but **depth export is not implemented** — `--with-depth` exits with an error (the `.rrd` stays the depth artifact of record) |

Scalars are **forward-filled** onto the union of scalar `frame_idx`; each state frame takes the
**nearest-earlier** RGB frame (images are decimated 1/N in the `.rrd`).

## Usage
```bash
python3 rrd_to_lerobot.py <in.rrd> --out <dir> [--fps 10] [--task "follow the locked person"] \
                          [--repo-id local/k1_follow] [--raw] [--allow-no-track]
```
The episode is **trimmed to start at the first real TRACK frame** (`/follow/range` sample) — a
TRACK-less `.rrd` is refused (else the forward-fill would fabricate `range=0.0` rows that read as
"at the robot"); pass `--allow-no-track` for a health/FSM-only export. Images are paired
**nearest-earlier only** (a frame before the first logged image gets `None`, never a future image).
- If the **`lerobot` library is importable**, the script hands off to `LeRobotDataset.create()` /
  `add_frame()` / `save_episode()` — **format-correct by construction**. Use this for a
  loader-conformant dataset.
- Otherwise (or with `--raw`) it writes a **documented RAW layout** that mirrors LeRobot v3
  field-for-field for inspection:
  ```
  <out>/meta/info.json | tasks.jsonl | episodes.jsonl | stats.json
  <out>/data/chunk-000/episode_000000.parquet          # state, action, timestamp, indices
  <out>/videos/chunk-000/observation.images.head_rgb/episode_000000.mp4
  ```
  The RAW layout is for pipeline inspection; for a dataset the LeRobot loader accepts, install
  `lerobot` and let the canonical writer produce it.

## Video backend
mp4 needs `imageio` + **ffmpeg** (`pip install imageio-ffmpeg`, or system ffmpeg). Without it the
script degrades to a **PNG frame folder** beside the mp4 path and records
`video_backend=png-fallback` in `info.json` — inspectable, just not a video.

## Validation status
- **Read + state/action assembly + RAW write: VALIDATED on a REAL robot `.rrd`** (2026-07-04, a 137 MB
  live `--rerun` preview recording): 417 RGB + **124 depth** frames extracted (448×544 head cam),
  `state (247,7)` (real `depth_fps`=13.1 column), `action (247,2)`, parquet + meta correct. This
  validation also caught a real sink bug (`SEARCH_MARKER` FSM state unmapped → -1). Pipeline proven.
- **mp4 encode: VALIDATED** — with `imageio-ffmpeg` installed, real h264 (544×448 @ 10 fps, 247 robot
  frames, readable back with imageio). Without ffmpeg it degrades to the PNG folder as designed.
- **Canonical `lerobot` path:** written to the documented API but **NOT run** (library not installed
  here). Validate against the installed `lerobot` version before trusting byte-level v3 conformance —
  the format is perishable.
- **A depth-valid FOLLOWING episode** (non-zero range/vx/vyaw): still needs a locked TRACK run on the
  robot (gesture lock didn't fire in the first attempt; retry with `--gesture-debug`).
