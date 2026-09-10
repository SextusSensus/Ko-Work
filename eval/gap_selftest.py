#!/usr/bin/env python3
"""Headless selftest for GAP STEERING's decision law: when it may engage, which way it turns, and
how hard. No robot, no motion, no camera -- the real node's _corridor_clearance() and
_gap_steer_bias() driven with authored depth frames, the same pattern as sector_selftest.py.

WHY THIS EXISTS (2026-09-10, items gap-01 / gap-02 of the read-only gap analysis):
  gap-01  the engage threshold (0.70 + 0.675*0.80 = 1.24 m) sat ABOVE the most the corridor can
          ever report (obstacle_brake_start 1.15 m -- a clear frame votes exactly that), so a
          clear corridor never released gap steer and the profile steered on clear frames.
  gap-02  with --gap-profile on, "no usable gap" fell through to the 3-sector test, which slams
          +/-gap_steer_rate toward any third that reads clear, with no width test.

Scenes are authored in the head camera's own geometry (544x448, --hfov-deg, bearing POSITIVE to
the RIGHT, as bearing_from_x) and chosen so the corridor gives the same answer with or without
scipy: the clear scene has NO return inside the robot's footprint, so the corridor takes the
"window empty, clear" path and votes exactly obstacle_brake_start either way. The config is the
SHIPPED defaults.yaml plus the app's gap-steer flags, i.e. the law the robot actually runs.

Cases:
  1 clear corridor   -- no footprint return, open room to the left -> 0 yaw, no commitment;
                        an expired commitment is released; an open room -> 0
  2 fitting gap LEFT -- centre blocked, gap at ~-19 deg -> +yaw == -k_yaw*gap, below the rail;
                        gap at ~-33 deg -> exactly gap_steer_rate, never above
  3 all too narrow   -- centre blocked, only slits (the LEFT third still reads clear, which is
                        what the sector fallback slammed toward) -> 0 yaw when not critical;
                        --gap-profile off keeps the sector path; a head-scan hint does not
                        reopen it
  4 critical         -- clearance 0.80 m, widest rejected opening on the RIGHT -> turns RIGHT at
                        <= half gap_steer_rate; mirrored -> LEFT; small bearing -> proportional;
                        nothing open at all -> 0; boxed in with a fresh head-scan hint -> the
                        hint, on its old gate
  5 gap dead ahead   -- a low box blocks the corridor, the band is open straight on -> 0 and no
                        side latch (a stale latch is cleared)

Usage:  python gap_selftest.py [--node <follow_person_k1.py>]
Off-robot, put the import-only rclpy/sensor_msgs stubs on PYTHONPATH.
"""
import argparse
import importlib.util
import math
import os
import sys
import time

import numpy as np

NODE_DEFAULT = "/home/booster/follow_person_k1.py"
H, W = 448, 544          # head depth frame
FAR = 4.0                # open space: past the 1.375 m gap horizon, inside obstacle_max_m (5.0)
BEYOND = 6.0             # past obstacle_max_m: no return at all
# The app's gap-steer flags (desktop/K1Finder.ps1 Get-TrackExtraArgs, gap = on). Everything else
# comes from robot/config/defaults.yaml.
APP_GAP = ["--gap-steer", "on", "--gap-steer-rate", "0.24", "--gap-steer-max-bearing-deg", "40"]
FX = None                # focal length (px), set from the node's own --hfov-deg in main()


def load(node_path):
    sys.path.insert(0, os.path.dirname(os.path.abspath(node_path)))
    spec = importlib.util.spec_from_file_location("follow_person_k1", node_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log = lambda *a, **k: None
    return m


def live_session():
    """True if a follow session is DRIVING. Same test as sector_selftest.py: the node running with
    --drive and not us (our own argv holds the node path, so a bare pgrep matches this process)."""
    try:
        import subprocess
        r = subprocess.run(["pgrep", "-f", "follow_person_k1.py"],
                           capture_output=True, text=True, timeout=10)
        me = {os.getpid(), os.getppid()}
        for tok in r.stdout.split():
            if not tok.isdigit() or int(tok) in me:
                continue
            try:
                with open("/proc/%s/cmdline" % tok, "rb") as fh:
                    cl = fh.read().decode("utf-8", "ignore").replace("\0", " ")
            except OSError:
                continue
            if "--drive" in cl and "selftest" not in cl:
                return True
        return False
    except Exception:  # noqa: BLE001 -- no pgrep (off-robot) -> no session to protect
        return False


class FakeNode:
    """Only the accessors the corridor and the gap profile touch."""

    def __init__(self, d):
        self.d = d

    def latest_depth(self, max_age=0.5):
        return self.d

    def head_yaw(self, max_age=0.5):
        return None          # head unknown -> no pan shift

    def latest_odom(self, max_age=1.0):
        return None          # no pose -> the memory layers write nothing


def col(deg):
    """Image column of a body-frame bearing in degrees (POSITIVE = RIGHT)."""
    return int(round(W / 2.0 + FX * math.tan(math.radians(deg))))


def frame(base, spans=()):
    """`base` metres everywhere, then each (deg0, deg1, metres) span painted over every row."""
    d = np.full((H, W), base, dtype=np.float32)
    for a0, a1, z in spans:
        d[:, max(0, col(a0)):min(W, col(a1))] = z
    return d


def decide(m, depth, prime=None, extra=(), hint=0):
    """A FRESH Follower (no vote history, no latch), one frame, operator dead ahead at 3 m.
    Returns (follower, corridor clearance, gap yaw). `prime` pre-sets (dir, age_s, mag); `hint`
    plants a fresh head-scan result (+1 LEFT / -1 RIGHT); `extra` appends launch flags."""
    f = m.Follower(m.parse_args(APP_GAP + list(extra)))
    if prime is not None:
        f._gap_dir, f._gap_dir_t, f._gap_mag = prime[0], time.monotonic() - prime[1], prime[2]
    if hint:
        f._scan_hint, f._scan_hint_t = hint, time.monotonic()
    f.node = FakeNode(depth)
    clr = f._corridor_clearance()
    return f, clr, f._gap_steer_bias(0.0, clr, 3.0)


def fm(x):
    return "None" if x is None else "%+.4f" % x


def dg(rad):
    return "None" if rad is None else "%+.1fdeg" % math.degrees(rad)


def main():
    global FX
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default=NODE_DEFAULT)
    ap.add_argument("--force", action="store_true",
                    help="run even while a follow session is live (do not: GPU contention)")
    a = ap.parse_args()
    if live_session() and not a.force:
        print("REFUSING: a follow session is live -- building the node contends for the GPU and "
              "can stall the bridge ping. Re-run when the session ends, or pass --force.")
        return 2
    m = load(a.node)
    c = m.parse_args(APP_GAP)
    FX = (W / 2.0) / math.tan(math.radians(c.hfov_deg) / 2.0)
    rate, k, bstart, crit = c.gap_steer_rate, c.k_yaw, c.obstacle_brake_start, c.gap_critical_m
    dead = math.radians(c.gap_steer_deadband_deg)
    crit_cap = max(c.gap_max_detour_deg, c.gap_critical_detour_deg)
    print("config: brake_start %.2f  gap_horizon %.3f  trigger_frac %.2f  rate %.2f  k_yaw %.3f  "
          "critical %.2f  deadband %.0fdeg  profile %s"
          % (bstart, c.gap_horizon_m, c.gap_steer_trigger_frac, rate, k, crit,
             c.gap_steer_deadband_deg, c.gap_profile))
    fails = []

    def check(name, ok, got):
        print("  %-60s %-18s %s" % (name, got, "OK" if ok else "FAIL"))
        if not ok:
            fails.append(name)

    # --- 1: gap-01 -- the clear vote must release ------------------------------------------------
    print("\ncase 1 -- clear corridor (the clear vote must release):")
    # Straight ahead: nothing inside obstacle_max_m. Left: an open room at 3 m. Right: a shelf at
    # 1 m. Neither side is inside the robot's footprint, so the corridor votes clear -- yet the band
    # profile sees the centre as not-passable and the room as a fitting gap well off-centre.
    d1 = frame(BEYOND, [(-60, -15, 3.0), (25, 60, 1.0)])
    f, clr, yaw = decide(m, d1)
    g = f._gap_profile(target_bearing=0.0)
    check("corridor votes clear: exactly obstacle_brake_start",
          clr is not None and abs(clr - bstart) < 1e-9, fm(clr))
    check("...while the profile WOULD steer (gap outside the deadband)",
          g is not None and abs(g[0]) > dead, dg(None if g is None else g[0]))
    check("no yaw on a clear corridor", yaw == 0.0, fm(yaw))
    check("no commitment made", f._gap_dir == 0, f._gap_dir)
    f, clr, yaw = decide(m, d1, prime=(1, 60.0, rate))
    check("an expired commitment is released by the clear vote",
          yaw == 0.0 and f._gap_dir == 0, "%s dir=%d" % (fm(yaw), f._gap_dir))
    f, clr, yaw = decide(m, frame(FAR))
    check("open room (everything far) -> no yaw", yaw == 0.0, fm(yaw))

    # --- 2: a gap that fits -> proportional, toward it, bounded -------------------------------
    print("\ncase 2 -- centre blocked at 1 m, a gap that FITS on the left:")
    f, clr, yaw = decide(m, frame(1.0, [(-29, -9, FAR)]))
    g = f._gap_profile(target_bearing=0.0)
    gb = None if g is None else g[0]
    check("corridor blocked, not critical", clr is not None and crit < clr < bstart, fm(clr))
    check("profile's gap is on the LEFT, outside the deadband",
          gb is not None and gb < -dead, dg(gb))
    check("turns LEFT (positive vyaw)", yaw > 0.0, fm(yaw))
    check("proportional: yaw == -k_yaw * gap bearing",
          gb is not None and abs(yaw - (-k * gb)) < 1e-9, fm(yaw))
    check("below the rail", 0.0 < yaw < rate, fm(yaw))
    check("commits LEFT", f._gap_dir == 1, f._gap_dir)
    f, clr, yaw = decide(m, frame(1.0, [(-43, -23, FAR)]))
    g = f._gap_profile(target_bearing=0.0)
    gb = None if g is None else g[0]
    check("a gap far enough off that -k_yaw*gap exceeds the rate",
          gb is not None and -k * gb > rate, dg(gb))
    check("...is capped at exactly gap_steer_rate, never above", yaw == rate, fm(yaw))

    # --- 3: gap-02 -- no usable gap, not critical -> no yaw (no sector slam) -------------------
    print("\ncase 3 -- centre blocked at 1 m, every opening too NARROW, not critical:")
    # The left third is open space broken by 2 px posts, one every third bin. A post fills >12% of
    # its bin, so the bin reads blocked and every run is ~2 bins -- far too narrow to fit. But the
    # posts are ~6% of the third's pixels, so its 12th percentile reads CLEAR: exactly the case the
    # old sector fallback slammed toward at full rate.
    d3 = np.full((H, W), 1.0, dtype=np.float32)
    third = int(W * c.sector_frac)
    d3[:, :third] = FAR
    bw = W / float(c.gap_profile_bins)
    for b in range(1, int(third / bw), 3):
        cc = int(b * bw + bw / 2.0)
        d3[:, cc - 1:cc + 1] = 1.0
    f, clr, yaw = decide(m, d3)
    s = f._sector_clearances()
    check("corridor blocked, not critical", clr is not None and crit < clr < bstart, fm(clr))
    check("profile finds no usable gap", f._gap_profile(target_bearing=0.0) is None, "None")
    check("...though the LEFT third reads clear (the old slam target)",
          s["L"] is not None and s["L"] >= f._gap_horizon(), fm(s["L"]))
    check("no yaw -- the brake governs vx", yaw == 0.0, fm(yaw))
    check("no commitment made", f._gap_dir == 0, f._gap_dir)
    f, clr, yaw = decide(m, d3, extra=["--gap-profile", "off"])
    check("--gap-profile off keeps the legacy sector path (+rate)", yaw == rate, fm(yaw))
    f, clr, yaw = decide(m, d3, extra=["--head-scan", "on"], hint=-1)
    check("a fresh head-scan hint does not reopen it (a side reads clear)", yaw == 0.0, fm(yaw))

    # --- 4: gap-02 -- critical -> bounded turn toward the widest rejected opening -------------
    print("\ncase 4 -- CRITICAL (clearance 0.80 m), nothing fits:")
    f, clr, yaw = decide(m, frame(0.8, [(-30, -27, FAR), (15, 27, FAR)]))
    wb = getattr(f, "_gap_widest_rej", None)
    check("corridor is critical", clr is not None and 0.0 < clr <= crit, fm(clr))
    check("profile finds no usable gap (critical detour cap)",
          f._gap_profile(target_bearing=0.0, max_detour_deg=crit_cap) is None, "None")
    check("widest rejected opening is the RIGHT one (15..27deg)",
          wb is not None and math.radians(13.0) < wb < math.radians(29.0), dg(wb))
    check("turns RIGHT (negative vyaw)", yaw < 0.0, fm(yaw))
    check("at no more than half gap_steer_rate", abs(yaw) <= 0.5 * rate + 1e-12, fm(yaw))
    check("yaw == -min(rate/2, k_yaw*|opening|)",
          wb is not None and abs(yaw + min(0.5 * rate, k * abs(wb))) < 1e-9, fm(yaw))
    check("commits RIGHT", f._gap_dir == -1, f._gap_dir)
    f, clr, yaw = decide(m, frame(0.8, [(-27, -15, FAR), (27, 30, FAR)]))
    check("mirrored (widest on the LEFT) -> turns LEFT, <= half rate",
          0.0 < yaw <= 0.5 * rate + 1e-12, fm(yaw))
    f, clr, yaw = decide(m, frame(0.8, [(8, 14, FAR)]))
    wb = getattr(f, "_gap_widest_rej", None)
    check("small bearing -> proportional, below the half-rate cap",
          wb is not None and wb > dead and abs(yaw + k * wb) < 1e-9 and -0.5 * rate < yaw < 0.0,
          "%s at %s" % (fm(yaw), dg(wb)))
    f, clr, yaw = decide(m, frame(0.8))
    check("nothing open at all -> no yaw (never a guess)",
          clr is not None and clr <= crit and yaw == 0.0 and f._gap_dir == 0, fm(yaw))
    f, clr, yaw = decide(m, frame(0.8), extra=["--head-scan", "on"], hint=1)
    check("boxed in + fresh head-scan hint -> the hint, as before (+rate)", yaw == rate, fm(yaw))

    # --- 5: a gap dead ahead is not a detour (86ce1b4 regression) -----------------------------
    print("\ncase 5 -- a gap DEAD AHEAD (inside the deadband):")
    # A LOW box 1 m ahead, entirely below the profile's row band: the corridor (full-height hit box)
    # sees it, the band does not -- the measured 'corridor blocked, forward open' class. A wall on
    # the far left makes the open run slightly asymmetric, so the deadband is what stands it down.
    d5 = frame(FAR, [(-60, -45, 1.0)])
    yb = int(H * c.obstacle_band_bot)
    d5[yb + 4:yb + 60, col(-20):col(20)] = 1.0
    f, clr, yaw = decide(m, d5, prime=(1, 60.0, rate))
    g = f._gap_profile(target_bearing=0.0)
    gb = None if g is None else g[0]
    check("corridor blocked, not critical", clr is not None and crit < clr < bstart, fm(clr))
    check("profile's gap is dead ahead (within the deadband)",
          gb is not None and abs(gb) <= dead, dg(gb))
    check("no yaw", yaw == 0.0, fm(yaw))
    check("no side latch (the stale one is cleared)",
          f._gap_dir == 0 and f._gap_mag == 0.0, "dir=%d mag=%.2f" % (f._gap_dir, f._gap_mag))

    print("")
    if fails:
        print("GAP-SELFTEST-FAIL %d: %s" % (len(fails), "; ".join(fails)))
        return 1
    print("GAP-SELFTEST-OK clear-vote release + proportional/bounded turns + no sector slam")
    return 0


if __name__ == "__main__":
    sys.exit(main())
