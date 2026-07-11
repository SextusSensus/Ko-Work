#!/usr/bin/env python3
"""label_run.py (P6.4) -- auto-label an offloaded run bundle with the ground-truth task-success
scorer, filling the outcome slot P6.3 reserves. Reuses the EXACT P5.1 scorer (score_outcome +
_TRACK_RE from replay_eval) -- no softer LLM/VLM judging; the label is the deterministic scorer's
output over the run's own decision log (the bundle's k1_follow.err), nothing else.

Runs on the WORKSTATION where Python lives (the repo home laptop has no Python -> run it on the
desktop, or call it from the offload receive path). Filesystem + JSON only; no daemon, no watcher.

  python label_run.py [--runs-dir DIR] [--run ID | --all] [--force]

Writes into each bundle's manifest.json a `label` block {outcome, metrics, oc, scorer_version,
labeled_utc} and a top-level `outcome` string (pass|review|incomplete) that Runs.ps1 surfaces into
runs/index.json. IDEMPOTENT: a bundle already labeled with this scorer_version is skipped unless
--force; bumping SCORER_VERSION triggers a clean relabel. Artifact-incomplete bundles (missing
k1_follow.err, or a drive that never locked -> 0 TRACK frames) are flagged `incomplete` for human
review, never silently labeled `pass`.

The metrics block also carries `fsm_occupancy` -- per-FSM-state tick counts read from the bundle's
events.jsonl `fsm` field, canonicalized via the shared eval/fsm_groups.py helper (SCAFFOLD_CHARTER
B1: the run-index coverage-gap signal Runs.ps1/index.json consumers schedule against). Occupancy is
ADVISORY for run scheduling; the dataset card's .rrd-derived per-state counts (batch_ingest.py, same
helper) stay AUTHORITATIVE for training (charter Section 5.2). A missing/corrupt events.jsonl
degrades to fsm_occupancy absent + a review flag, never a crash.
"""
import argparse
import json
import os
import re
import sys
import time as _time

# Reuse the real P5.1 scorer. replay_eval's module-level imports are pure stdlib (rclpy/YOLO load
# lazily inside functions), so importing these two names never pulls a robot dependency.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from replay_eval import score_outcome  # noqa: E402
# The ONE fsm-grouping truth (charter B1): id/string -> canonical state name. Never hand-roll a map.
from fsm_groups import count_strings  # noqa: E402

# Bump this when the scorer, the default thresholds, or the label CONTENT changes (e.g. a new
# metrics field like fsm_occupancy) -> a relabel pass supersedes old labels.
SCORER_VERSION = "p5.1-score_outcome+fsm-occupancy/2026-07-10"

# Default outcome thresholds for a LIVE run (no per-clip manifest, no ground-truth operator_id -> the
# scorer falls back to ID STABILITY, not ID CORRECTNESS -- see score_outcome docstring). standoff_m /
# geofence_m are refined from the bundle's own config below when readable. Safety metrics are strict
# (any forbidden-forward or geofence breach -> flagged for review, per "flag anomalies, don't guess").
DEFAULT_OC = {
    "standoff_m": 1.2,
    "standoff_band_m": 0.4,
    "min_standoff_in_band_frac": 0.6,
    "geofence_m": 3.0,
    "max_geofence_breach": 0,
    "forward_eps": 0.02,
    "max_forbidden_forward": 0,
    "max_id_switches": 8,
}

# Minimal flat-YAML scalar reader for the few numeric keys we refine from the bundle's config --
# avoids a pyyaml dependency (the config is `key: value` lines).
def _yaml_scalar(path, key):
    try:
        with open(path) as f:
            for ln in f:
                m = re.match(r"\s*%s\s*:\s*([-+0-9.]+)" % re.escape(key), ln)
                if m:
                    return float(m.group(1))
    except Exception:
        pass
    return None


def _rrd_thresholds(bundle):
    """Read the effective standoff/geofence the node RECORDED as static refs in the .rrd
    (/follow/range/standoff and /follow/range/max, written by k1_rerun.RerunSink.refs_once). This is
    the SELF-DESCRIBING, authoritative source: it reflects the values actually in effect that run and
    is robust to an on-robot build that predates the `CONFIG ...` text line (the deploy can lag the
    labeler). refs_once logs each ref only when its value > 0, so a recorded standoff with an ABSENT
    /follow/range/max means the geofence was DISABLED (max_follow_range=0) that run -> geofence None.
    Returns {} when there is no .rrd, rerun is unavailable, or no standoff ref was recorded (caller
    then keeps the config-file / CONFIG-line values)."""
    rrd = None
    try:
        for n in sorted(os.listdir(bundle)):
            if n.endswith(".rrd"):
                rrd = os.path.join(bundle, n)
                break
    except OSError:
        return {}
    if rrd is None:
        return {}
    try:
        from rerun.experimental import RrdReader
    except Exception:
        return {}                       # no rerun in this env -> graceful fall-back to text sources
    try:
        store = RrdReader(rrd).stream().collect()
        vals = {}
        for ch in store.stream():
            if not ch.is_static:
                continue
            ep = str(ch.entity_path)
            if ep not in ("/follow/range/standoff", "/follow/range/max"):
                continue
            rb = ch.to_record_batch()
            i = rb.schema.get_field_index("Scalars:scalars")
            if i < 0:
                continue
            col = rb.column(i).to_pylist()
            v = col[0] if col else None
            if isinstance(v, (list, tuple)):
                v = v[0] if v else None
            if v is not None:
                vals[ep] = float(v)
    except Exception:
        return {}
    if "/follow/range/standoff" not in vals:
        return {}                       # refs_once did not record -> not a self-describing .rrd
    return {"standoff_m": vals["/follow/range/standoff"],
            "geofence_m": vals.get("/follow/range/max")}    # absent => None => geofence disabled


def build_oc(bundle):
    """DEFAULT_OC refined from (a) the bundle's config YAMLs, then (b) -- AUTHORITATIVE -- the effective
    thresholds the run itself recorded: the node's `CONFIG standoff_m=.. max_follow_range=..` line in
    k1_follow.err, else (fallback, robust to a stale node build) the /follow/range/standoff + /max
    static refs in the .rrd. App runs set these via CLI (not a bundled profile), so config-file
    defaults alone would mislabel a clean run. Returns the oc dict actually used (recorded in the label)."""
    oc = dict(DEFAULT_OC)
    cfgdir = os.path.join(bundle, "config")
    layers = [os.path.join(cfgdir, "defaults.yaml")]
    # manifest.profile may name a real profile (demo/field/capture) whose config was bundled; app runs
    # use synthetic labels (tracker-drive) with no matching file -> just skip the overlay.
    prof = _read_manifest(bundle).get("profile", "")
    cand = os.path.join(cfgdir, "%s.yaml" % prof)
    if os.path.isfile(cand):
        layers.append(cand)
    for path in layers:
        so = _yaml_scalar(path, "standoff_m")
        gf = _yaml_scalar(path, "max_follow_range")
        if so is not None:
            oc["standoff_m"] = so
        if gf is not None and gf > 0:
            oc["geofence_m"] = gf
    # AUTHORITATIVE overlay #1 (cheap): the effective thresholds the node logged this run (P6.4).
    config_line_found = False
    err = os.path.join(bundle, "k1_follow.err")
    if os.path.isfile(err):
        try:
            with open(err, errors="replace") as f:
                for ln in f:
                    m = re.search(r"CONFIG standoff_m=([-\d.]+) max_follow_range=([-\d.]+)", ln)
                    if m:
                        oc["standoff_m"] = float(m.group(1))
                        gf = float(m.group(2))
                        oc["geofence_m"] = gf if gf > 0 else None  # 0 == geofence disabled this run
                        config_line_found = True
                        break
        except Exception:
            pass
    # AUTHORITATIVE overlay #2 (fallback, robust to a stale node build that lacks the CONFIG line):
    # the standoff/geofence the run RECORDED as static refs in its .rrd. Only read the .rrd when the
    # cheap CONFIG line was absent (the legacy-.rrd scan is not free), so this stays fast once the
    # on-robot build catches up. This is what makes labeling self-describing for ANY standoff.
    if not config_line_found:
        th = _rrd_thresholds(bundle)
        if th:
            oc["standoff_m"] = th["standoff_m"]
            oc["geofence_m"] = th["geofence_m"]   # None => geofence disabled that run
    return oc


def _read_manifest(bundle):
    try:
        with open(os.path.join(bundle, "manifest.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def _fsm_occupancy(bundle):
    """Per-FSM-state tick counts from the bundle's events.jsonl `fsm` field (P5.3 writes one JSON
    line per control tick), keyed by CANONICAL state name via the shared fsm_groups helper -- so
    this index-side count and batch_ingest's card-side count can never disagree on grouping. An
    UNMAPPED key here means a data bug upstream (surfaced, not judged -- the .rrd ingest is the
    hard gate). Returns None -- NEVER raises -- when events.jsonl is missing or yields no parseable
    fsm entries; the caller degrades to fsm_occupancy absent + flag-for-review. Torn tail lines
    (the JSONL is line-buffered, a hard crash truncates mid-line) are skipped, not fatal."""
    path = os.path.join(bundle, "events.jsonl")
    if not os.path.isfile(path):
        return None
    states = []
    try:
        with open(path, errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    ev = json.loads(ln)
                except ValueError:
                    continue        # torn/corrupt line -> skip it, count the rest
                if isinstance(ev, dict) and isinstance(ev.get("fsm"), str):
                    states.append(ev["fsm"])
    except Exception:
        return None
    if not states:
        return None                 # a tick log with zero fsm entries is as bad as no log
    return count_strings(states)


def label_bundle(bundle, force=False):
    """Score one bundle dir; write the label into its manifest.json. Returns the outcome string."""
    man_path = os.path.join(bundle, "manifest.json")
    man = _read_manifest(bundle)
    if not man:
        print("label: %s -- no manifest.json, skipping" % os.path.basename(bundle))
        return None
    existing = man.get("label")
    if existing and existing.get("scorer_version") == SCORER_VERSION and not force:
        print("label: %s -- already %s (scorer %s), skip" %
              (man.get("run_id"), existing.get("outcome"), SCORER_VERSION))
        return existing.get("outcome")

    err = os.path.join(bundle, "k1_follow.err")
    if not os.path.isfile(err):
        outcome, metrics = "incomplete", {"reason": "no k1_follow.err in bundle"}
    else:
        with open(err, errors="replace") as f:
            lines = f.readlines()
        oc = build_oc(bundle)
        passed, metrics = score_outcome(lines, oc)
        metrics["oc"] = oc
        if metrics.get("track_frames", 0) == 0:
            # A run that never LOCKed a person can't be scored for follow success -- flag, don't pass.
            outcome = "incomplete"
            metrics.setdefault("fails", []).append("no locked-TRACK frames -> not assessable")
        else:
            outcome = "pass" if passed else "review"

    # B1 occupancy (SCAFFOLD_CHARTER): per-state tick counts for the run-index coverage-gap signal.
    # events.jsonl is written by EVERY run (P5.3 always-on, bundled by offload_run.sh), so its
    # absence is itself an anomaly: occupancy degrades to ABSENT and a clean `pass` is demoted to
    # `review` (flag anomalies, don't guess) -- scoring above is never blocked and this never raises.
    occ = _fsm_occupancy(bundle)
    if occ is not None:
        metrics["fsm_occupancy"] = occ
    else:
        metrics.setdefault("fails", []).append("events.jsonl missing/unreadable -> no fsm_occupancy")
        if outcome == "pass":
            outcome = "review"

    man["label"] = {
        "outcome": outcome,
        "scorer_version": SCORER_VERSION,
        "labeled_utc": _time.strftime("%Y%m%dT%H%M%SZ", _time.gmtime()),
        "metrics": metrics,
    }
    man["outcome"] = outcome    # top-level slot Runs.ps1 surfaces into index.json
    tmp = man_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(man, f, indent=2, sort_keys=True)
    os.replace(tmp, man_path)
    fails = metrics.get("fails", [])
    print("label: %s -> %s%s" % (man.get("run_id"), outcome,
                                 (" (" + "; ".join(fails) + ")") if fails else ""))
    return outcome


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs-dir", default="runs", help="the workstation runs/ store (default: runs)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--run", help="label a single run_id")
    g.add_argument("--all", action="store_true", help="label every bundle (default if neither given)")
    ap.add_argument("--force", action="store_true", help="relabel even if scorer_version matches")
    a = ap.parse_args()

    if not os.path.isdir(a.runs_dir):
        raise SystemExit("no such runs dir: %s" % a.runs_dir)
    if a.run:
        bundles = [os.path.join(a.runs_dir, a.run)]
    else:
        bundles = sorted(os.path.join(a.runs_dir, d) for d in os.listdir(a.runs_dir)
                         if os.path.isdir(os.path.join(a.runs_dir, d)) and not d.startswith("."))
    if not bundles:
        print("label: no bundles under %s" % a.runs_dir)
        return 0
    counts = {}
    for b in bundles:
        res = label_bundle(b, force=a.force)
        counts[res] = counts.get(res, 0) + 1
    print("label: done -- " + ", ".join("%s=%d" % (k, v) for k, v in sorted(counts.items()) if k))
    return 0


if __name__ == "__main__":
    sys.exit(main())
