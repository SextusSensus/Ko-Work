# Council review — robot↔pipeline alignment + robustness (2026-07-14)

Multi-agent adversarial review (`wf_f2789658`, 22 agents, 5 dimensions → verify → synthesize) of whether
the K1 robot code aligns with the desktop pipeline that runs on its data, plus splat/recon/trainer
robustness. **16 findings raised → 9 CONFIRMED, 7 refuted** (adversarial verification killed the 7).

## The reassuring headline

The safety finder **CONFIRMED the core invariant holds**: there is **no code path from any shadow /
learned-policy node to the locomotion bridge — it is ABSENT, not flagged off**. The only ONNX/TRT models
on the robot are YOLO/ReID perception. The deployed safety spine (velocity slew, depth floor + geofence,
deadman/heartbeat, watchdog, lost-track coast) matches the graded ladder the P7.6 shield spec assumes.

## Fixed overnight (desktop-side — code that runs here, low behavior risk)

All on branch `desktop/overnight-hardening`, each tested (see the commit trailer for the selftest run).

| # | Finding | Fix | File |
|---|---|---|---|
| F4 | Many depth frames pair to ONE decimated RGB → photometric odometry degenerates, mesh smears, nothing in the manifest shows it (likely a real cause of tonight's mesh) | Record `unique_rgb_frames` / `duplicate_rgb_pairs` + a loud `PAIR-WARN` | `desktop/recon/geom.py` |
| F5 | A png-fallback / truncated mp4 silently trains the CNN on black/frozen frames; every other gate stays green | Refuse a non-`mp4` `video_backend` episode up front + strict decoded-frame check (1-frame torn-tail tolerance) | `eval/train_act.py` |
| F7 | Ingest decodes the whole `.rrd` (incl. RGB it throws away) + concatenates every depth pixel for a median → OOM risk on a long capture; ingest crash left no manifest | `read_rrd(depth_only=True)` skips RGB; bounded subsampled median; ingest wrapped like the geometry stages so any crash still writes a manifest | `desktop/recon/cli.py`, `eval/rrd_to_lerobot.py` |
| F8 | CoACD has no timeout → a pathological real mesh hangs the container forever with no manifest (native, uncatchable in Python) | Worker wall-clock kill (`K1_JOB_TIMEOUT_S`, 90 min) turns any hang into a loud failed job; CoACD pre-clean (degenerate/duplicate/non-manifold removal) bounds the input | `cluster/worker.py`, `desktop/recon/geom.py` |
| F2 | Two contradictory **binding** P7.6 graduation specs; the newer cites a `|vx|≈0.15` clamp that doesn't exist (deployed is `vx∈[−0.06,+0.18]`, hard ±0.30) | Marked the 2026-07-10 draft SUPERSEDED by the 2026-07-13 BINDING entry; corrected the clamp to the real asymmetric values | `DECISIONS.md` |

## ⚠ NOT fixed — needs you (robot code = deploy risk, or a semantics decision)

I did **not** touch robot code or change dataset semantics unsupervised. Ranked by leverage:

1. **[HIGH — the top alignment risk] `/cmd/vx,/cmd/vyaw` are logged ONLY on TRACK frames**
   (`robot/follow_person_k1.py:2369`). Non-TRACK frames (SEARCHING/REACQUIRE/PARKED) forward-fill the
   last TRACK command, so the trainer learns to **drive forward exactly when the robot actually stands
   still** — the opposite of safe — and the per-state REACQUIRE MSE is invented, not measured.
   **Fix:** emit the true per-tick command (already in `self._ev_cmd`, post-clamp) every control
   iteration next to the `/fsm/state_id` log (~L1150), not only inside `_track`. **One edit.**
2. **[MEDIUM, same seam] `/reid/sim`, `/track/conf`, `/follow/range` are also TRACK-only**
   (`follow_person_k1.py:2372`) → lost-track frames present a stale "confident lock" observation. The
   SAME one-line fix (emit real obs every tick) closes this too. This is the single highest-value robot
   edit; it directly answers "does the robot code align with the pipeline" — **right now it doesn't.**
3. **[HIGH] RGBD pairs on callback-receipt time, not the sensor stamp** (`robot/perception.py:305`):
   both RGB (283) and depth (305) stamp the rerun timeline with `time.time()`; `msg.header.stamp` is
   never read, so the 0.06 s pairing budget is spent on arrival-latency → mispaired color/depth
   (ghosting) or mass rejection on a moving robot. **Fix:** use `msg.header.stamp` for both.
4. **[HIGH, desktop-but-a-recon-behavior-decision] Odometry keyframes on RGB-distinct frames**
   (`desktop/recon/geom.py`): F4's deeper fix — run frame-to-frame odometry between distinct RGBs
   (skip duplicate-RGB depth for the odom step; keep them for TSDF density). Improves the trajectory
   but changes recon output, so it's your call. Tonight's change makes the degeneracy visible; this
   would fix it.
5. **[MEDIUM, low urgency] Bind `splits.json` to `stats.json`** (`eval/train_act.py:148`): a hand-edited
   split can silently train on stale train-only norm stats. Fix folds a sha256 of the sorted train
   run-ids into the mint + a loader assert (touches the dataset format → deferred, only trips under
   manual split curation).

## Capture takeaway (from tonight's splat A/B)

The depth-truncation fix moved held-out PSNR only 12.64 → 12.91 dB; freezing poses did nothing (12.65).
**Processing is not the bottleneck — the capture is.** The path to a good mesh+splat is a calibrated,
slow, room-coverage scan (P8.1 calibration + a look-around, not a person-follow), and multiple passes
merged (P8.3). Note #1/#2 above ALSO improve the *training* data from the same captures.
