# Replay eval — the `--drive` regression gate (P2 #14)

`replay_eval.py` runs recorded clips through the **real deployed follow stack** (YOLO +
appearance + identity + FSM) headless on the robot, captures the node's own decision-log
lines, and scores them. Motion-free by construction (forced `--preview`, no bridge; depth
stubbed off so forward drive is impossible). A synthetic clock makes replays deterministic,
so `compare` can demand **byte-identical** decision streams between flag-sets.

## Run (on the robot, ROS env sourced)
```bash
scp replay_eval.py clips.json booster@<ip>:/home/booster/
ssh booster@<ip>
source /opt/ros/humble/setup.bash; source /opt/booster/BoosterRos2/install/setup.bash

python3 replay_eval.py selftest                      # plumbing + determinism (no clips needed)
python3 replay_eval.py run     --clip clips/C.mp4 --flags "--auto-reacquire"
python3 replay_eval.py compare --clip clips/C.mp4 --flags-a "" --flags-b "--reid-fault-k 0"
python3 replay_eval.py expect  --manifest clips.json # the gate; exit!=0 on any FAIL
```

## Where clips come from
Every runbook session (`../ARM_VALIDATION_RUNBOOK.md`, post-session step) — save the
loss→relock sequences into `/home/booster/clips/` and add them to `clips.json` under the
A/B/C/E families (stubs inside). Grow the suite; never delete a clip that once caught a bug.

## Honest limits (sim-eval doctrine)
- A replay success rate is scored over **these clips**, not the world — it gates regressions,
  it does not prove field performance. Wilson intervals are printed; 4/4 ≠ 100%.
- `compare` proves equivalence between two flag-sets of the **current** build. Byte-identity
  to a *historical* build (true Stage-1 rollback) would need the old file checked out.
- TRT FP16 is deterministic in practice, not guaranteed bitwise; logged values are rounded.
  The no-person `selftest` is exactly deterministic.
