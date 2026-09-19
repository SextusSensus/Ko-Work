# Crowd 2FA lock (2026-09-11)

Follow ONE operator in a dense crowd and never hand the lock to anyone else.

## Why the old lock is not enough in a crowd

- A raised hand is something anyone in a crowd of 80 does by accident.
- The tracking floor (OSNet anchor similarity 0.35) keeps the lock sticky through turns and
  lighting, but in the Sep-10 logs strangers reached 0.68 against the operator's anchor. A tracker
  id handed to a stranger on a crossing, or a stranger near the last position while the operator is
  occluded, could be followed.
- Markerless auto re-lock is a probabilistic re-ID match; it cannot be a hard guarantee.

## What the crowd profile does (`--profile crowd`, K1Finder "Crowd 2FA lock")

Two independent factors guard every seed and re-seed:

1. **Something you DO** — an ordered gesture sequence, completed by ONE body, each step held
   `gesture_2fa_step_s` (0.6 s), no more than `gesture_2fa_gap_s` (2 s) between steps, the whole
   thing within `gesture_2fa_total_s` (12 s). Two bodies completing on the same frame are refused.
   The robot must be stood still (SEARCH / REACQUIRE / PARKED); it never runs while driving.
2. **Something you ARE** — after the first lock, every re-seed must match the frozen appearance
   anchor (`reseed_anchor_floor` 0.55). A stranger who copies the sequence is refused by identity.

And while following:

- A candidate on a **different tracker id** than the bound one is a change of body: it must match
  the anchor at `switch_anchor_floor` (0.55), the same bar as a re-seed. The bound body keeps the
  0.35 floor, so a turned or relit operator is not dropped.
- If the accepted body stays below that floor for `switch_anchor_frames` (8, about 0.8 s) the lock
  is declared LOST (`IDENTITY-DOUBT`), the robot stands, and only the sequence can re-seed.
- Auto re-lock is OFF; the node refuses to start with it on. The yaw search scan is off too (it
  cannot re-lock without auto re-lock, and the sequence needs a still robot).
- Brief occlusions are bridged (`coast_frames` 6, `lost_grace` 12) before a loss is declared.

## The sequences

**Body sequence (works today, pose model only):** stand facing the robot, then

1. right hand up (hold ~1 s)
2. both hands up (~1 s)
3. left hand up only (~1 s)
4. both hands down

**Finger countdown (needs the hand model):** one hand up, show 5 fingers, then 4, 3, 2, 1, fist,
holding each ~1 s. Set `gesture_2fa_steps: 'F5,F4,F3,F2,F1,F0'` and deploy the model as
`/home/booster/hand_kp.onnx`. Without the model file the node REFUSES a finger-step config.

### Hand model recipe

The robot's YOLO11n-pose sees 17 body joints and no fingers. The countdown reads a second model on
a small crop around the raised wrist (`robot/handcount.py`, 21 landmarks, MediaPipe layout).
On a GPU box:

```bash
pip install ultralytics
yolo pose train data=hand-keypoints.yaml model=yolo11n-pose.pt epochs=100 imgsz=640
yolo export model=runs/pose/train/weights/best.pt format=onnx imgsz=256 half=True
scp best.onnx booster@192.168.9.75:/home/booster/hand_kp.onnx
```

Then verify on the robot in PREVIEW: `HAND-COUNTER ok` in the log, and `GESTURE-2FA tid=.. step
k/6 Fn held` lines as you count down at 1.5-2 m. The crop is upscaled to 256 px, so a hand that
is ~30 px wide at 2 m becomes ~200 px for the model.

## Log lines

`GESTURE-2FA ok ...` (banner), `GESTURE-2FA tid=7 step 2/4 BOTH_UP held`, `GESTURE-2FA-RESET`,
`GESTURE-2FA-AMBIGUOUS`, `GESTURE-2FA-COMPLETE`, then the usual `LOCKED person seeded` or
`SEED-REJECT identity-mismatch`. `IDENTITY-DOUBT` marks a lock dropped on identity.

## Verification

- Offline: `python eval/gesture2fa_selftest.py` (40 checks, `GESTURE2FA-SELFTEST-OK`).
- Defaults are byte-identical: the default decision stream on the no-person clip matches the
  pre-change node; `parse_args` differs only by the 7 new keys.
- VERIFY ON ROBOT: preview first (no drive), one person; then two people where the second copies
  the sequence (must refuse by identity after the first lock); then a crossing.
