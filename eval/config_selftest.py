#!/usr/bin/env python3
"""Config-loader selftest (P1.1b) -- the negative/precedence assertions the byte-identical
parity harness (config_parity.ps1) cannot cover: appearance-floor resolution, type fidelity,
CLI-wins precedence (incl. an explicit CLI value equal to the node default still beating a
profile), and fail-closed rejection of unknown keys / bad choices / missing profiles.

Motion-free, headless, no robot. On the robot rclpy + PyYAML are present; off-robot run with
the _gate ROS stubs on PYTHONPATH. Prints CONFIG-SELFTEST-OK, or raises on the first failure.

Usage:  python config_selftest.py [--node <follow_person_k1.py>]
"""
import argparse, importlib.util, os, sys, tempfile

NODE_DEFAULT = "/home/booster/follow_person_k1.py"


def load(node_path):
    # P3: the node imports sibling modules (common.py, ...) -- add its dir to sys.path.
    sys.path.insert(0, os.path.dirname(os.path.abspath(node_path)))
    spec = importlib.util.spec_from_file_location("follow_person_k1", node_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log = lambda *a, **k: None
    return m


def expect_exit(fn, label):
    try:
        fn()
    except SystemExit:
        return
    raise AssertionError("expected SystemExit (fail-closed), got none: %s" % label)


def write(tmp, text):
    p = os.path.join(tmp, "prof.yaml")
    with open(p, "w") as f:
        f.write(text)
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default=NODE_DEFAULT)
    pa = load(ap.parse_args().node).parse_args

    # 1. defaults + appearance-resolved floors (global) + the not-drive->preview mutation
    d = vars(pa([]))
    assert d["appearance"] == "global", d["appearance"]
    assert d["anchor_floor"] == 0.50 and d["bank_floor"] == 0.55 and d["reloc_floor"] == 0.70
    assert d["hiconf"] == 0.55 and d["reloc_view_floor"] == 1.01 and d["reloc_iso_bypass_anchor"] == 0.65
    assert d["reloc_anchor_backstop"] == 0.40 and d["preview"] is True and d["drive"] is False
    # osnet resolves the SAME null floors differently
    o = vars(pa(["--appearance", "osnet"]))
    assert o["anchor_floor"] == 0.35 and o["bank_floor"] == 0.40 and o["reloc_floor"] == 0.55
    assert o["reloc_iso_bypass_anchor"] == 0.55 and o["reloc_anchor_backstop"] == 0.28

    # 2. type fidelity: YAML must not drift float<->int
    assert type(d["max_follow_range"]) is float and type(d["standoff_m"]) is float
    assert type(d["stream_quality"]) is int and type(d["coast_frames"]) is int

    # 2b. the profile branch: an empty profile (dev) is byte-identical to no profile
    assert vars(pa(["--profile", "dev"])) == d, "--profile dev must equal no-profile"

    with tempfile.TemporaryDirectory() as tmp:
        # 3. precedence: CLI wins over a conflicting profile; profile still applies where CLI absent
        prof = write(tmp, "standoff_m: 9.9\nvx_max: 0.01\nmax_follow_range: 3\n")
        r = vars(pa(["--profile", prof, "--standoff-m", "1.2"]))
        assert r["standoff_m"] == 1.2, r["standoff_m"]            # CLI wins
        assert r["vx_max"] == 0.01                                 # profile (no CLI override)
        assert r["max_follow_range"] == 3.0 and type(r["max_follow_range"]) is float  # int->float coercion

        # 4. explicit CLI value == node default STILL beats a profile-non-default (SUPPRESS mechanism)
        r2 = vars(pa(["--profile", prof, "--vx-max", "0.18"]))
        assert r2["vx_max"] == 0.18, r2["vx_max"]

        # 5-10. fail-closed rejections (incl. the adversarial-review hardening)
        expect_exit(lambda: pa(["--profile", write(tmp, "vx_maxx: 0.1\n")]), "unknown key")
        expect_exit(lambda: pa(["--profile", write(tmp, "appearance: foo\n")]), "bad choice")
        expect_exit(lambda: pa(["--profile", os.path.join(tmp, "nope.yaml")]), "missing profile")
        expect_exit(lambda: pa(["--profile", write(tmp, "stream_quality: 70.5\n")]),
                    "non-integer float for an int key (argparse would reject, not truncate)")
        expect_exit(lambda: pa(["--profile", write(tmp, "track: 'false'\n")]),
                    "non-bool YAML scalar for a bool flag (truthy-string trap)")
        expect_exit(lambda: pa(["--profile", write(tmp, "anchor_floor: 0.5\n")]),
                    "baked appearance-floor in a profile")

    # 11. P1.2 fail-closed drive gate: --drive without the deadman is refused; two explicit hatches run
    expect_exit(lambda: pa(["--drive"]), "drive without --require-heartbeat / override")
    assert vars(pa(["--drive", "--require-heartbeat"]))["drive"] is True      # safe hatch: deadman armed
    assert vars(pa(["--drive", "--allow-untethered-unsafe"]))["drive"] is True  # unsafe explicit override
    assert vars(pa([]))["allow_untethered_unsafe"] is False                   # default off (in defaults.yaml)

    print("CONFIG-SELFTEST-OK (defaults+floors, types, precedence, fail-closed, drive-gate)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
