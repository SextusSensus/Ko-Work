#!/usr/bin/env python3
"""fsm_groups.py -- the shared FSM-grouping helper: ONE source of grouping truth
(SCAFFOLD_CHARTER S1/B1 micro-module). Imported by label_run.py's occupancy patch,
batch_ingest.py's dataset-card writer, the S5 val report, and the future P7.5 grader --
two hand-rolled id->name maps are guaranteed to drift, so nobody else builds one.

Loads eval/fsm_states.json (the FROZEN categorical encoding of /fsm/state_id, the
version-locked mirror of robot/k1_rerun.py::_STATE_CODE) from beside this module and
exposes the canonical grouping surface:

  ID_TO_NAME            dict float id -> canonical state name (aliases excluded)
  NAME_TO_ID            dict state name (incl. aliases) -> float id
  CANONICAL_NAMES       tuple of canonical names, fsm_states.json order
  VERSION               fsm_states.json "version" -- record it wherever counts persist
  UNMAPPED              the sentinel name "UNMAPPED"; ALWAYS means a data bug upstream
  name_for_id(v, tol)   id -> canonical name; UNMAPPED for anything not within tol
                        (default 1e-6) of a frozen id, INCLUDING the writer's -1.0 sentinel
  name_for_string(s)    state string -> canonical name; unknown/non-string -> UNMAPPED
  count_ids(values)     iterable of ids -> {canonical name: count} (observed names only)
  count_strings(strs)   iterable of state strings -> {canonical name: count} (observed only)

The single SEARCH/SEARCH_MARKER rule (charter-pinned): the node's real search-state string
is "SEARCH_MARKER" (robot/common.py S_SEARCH); plain "SEARCH" is a defensive alias sharing
frozen id 1.0, so id 1.0 ALWAYS reports as canonical SEARCH_MARKER. SEARCHING (1.5, the
yaw-only re-find scan) is a DISTINCT state, never grouped with the search-for-lock state.
UNMAPPED is never a state: batch_ingest treats it as a mint-abort; label_run's occupancy
just surfaces it for review.

Stdlib only -- safe to import anywhere (laptop stubs, desktop, Jetson).

Self-test:  python eval/fsm_groups.py selftest   -> prints FSM-GROUPS-SELFTEST-OK
"""
import json
import os
import sys

UNMAPPED = "UNMAPPED"

# Names that INTENTIONALLY share a frozen id, alias -> canonical. Charter-pinned: SEARCH is
# the defensive alias of SEARCH_MARKER (see the fsm_states.json _comment). Any OTHER id
# collision in the JSON is unplanned -> refuse to load rather than silently pick a canonical
# name (fail-closed: a wrong grouping map corrupts every downstream count).
_ALIAS_OF = {"SEARCH": "SEARCH_MARKER"}

# Source of truth lives BESIDE this module (works from any cwd, matches label_run's
# sys.path idiom). A missing/garbled fsm_states.json raises at import -- that is a broken
# checkout, not a per-run condition, so failing loudly here is correct.
_STATES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fsm_states.json")
with open(_STATES_PATH) as _f:
    _DOC = json.load(_f)

VERSION = _DOC["version"]
NAME_TO_ID = {str(_n): float(_v) for _n, _v in _DOC["states"].items()}

ID_TO_NAME = {}
for _name, _fid in NAME_TO_ID.items():
    if _name in _ALIAS_OF:
        continue                    # an alias never becomes the canonical name for its id
    if _fid in ID_TO_NAME:
        raise RuntimeError(
            "fsm_states.json: states %r and %r share id %r with no alias rule in "
            "fsm_groups.py -- the two files are out of sync" % (ID_TO_NAME[_fid], _name, _fid))
    ID_TO_NAME[_fid] = _name

# Guard the alias table against a stale JSON: the canonical target must exist, and an alias
# present in the JSON must share its canonical's id -- otherwise grouping truth is broken.
for _alias, _canon in _ALIAS_OF.items():
    if _canon not in NAME_TO_ID or (
            _alias in NAME_TO_ID and NAME_TO_ID[_alias] != NAME_TO_ID[_canon]):
        raise RuntimeError(
            "fsm_groups.py alias %r -> %r contradicts fsm_states.json" % (_alias, _canon))

CANONICAL_NAMES = tuple(ID_TO_NAME.values())


def name_for_id(value, tol=1e-6):
    """Canonical state name for a recorded /fsm/state_id value. Returns UNMAPPED (a data
    bug, never a state) for anything not within tol of a frozen id -- including the
    recorder's -1.0 sentinel, NaN, and non-numeric junk. tol absorbs float32 round-trip
    jitter from the .rrd scalar path without ever bridging distinct ids (min gap is 0.5)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return UNMAPPED
    for fid, name in ID_TO_NAME.items():
        if abs(v - fid) <= tol:     # NaN fails every comparison -> falls through to UNMAPPED
            return name
    return UNMAPPED


def name_for_string(s):
    """Canonical state name for a node state STRING (events.jsonl `fsm` field, log text).
    Routes through the frozen id so aliasing has exactly one implementation:
    SEARCH -> id 1.0 -> SEARCH_MARKER. Unknown/non-string -> UNMAPPED."""
    if not isinstance(s, str):
        return UNMAPPED
    s = s.strip()
    if s not in NAME_TO_ID:
        return UNMAPPED
    return ID_TO_NAME[NAME_TO_ID[s]]


def count_ids(values):
    """{canonical name: count} over an iterable of recorded ids (python or numpy scalars).
    Counter semantics: only OBSERVED names appear -- consumers needing explicit zero floors
    (e.g. the S5 INSUFFICIENT rendering) iterate CANONICAL_NAMES themselves. An UNMAPPED
    key present at all means a data bug upstream."""
    counts = {}
    for v in values:
        name = name_for_id(v)
        counts[name] = counts.get(name, 0) + 1
    return counts


def count_strings(strings):
    """{canonical name: count} over an iterable of state strings (same semantics as
    count_ids: observed-only keys, aliases merged into their canonical bucket)."""
    counts = {}
    for s in strings:
        name = name_for_string(s)
        counts[name] = counts.get(name, 0) + 1
    return counts


def _selftest():
    # id path -- the frozen map, the tol window, and the UNMAPPED contract. These pins are
    # DELIBERATELY hard: if fsm_states.json ever changes, this selftest must fail so the
    # migration visits every consumer (per the JSON's own bump-and-migrate note).
    assert name_for_id(3.0) == "TRACK"
    assert name_for_id(2.0) == "REACQUIRE"
    assert name_for_id(1.5) == "SEARCHING"           # distinct: never grouped with search-for-lock
    assert name_for_id(1.0) == "SEARCH_MARKER"       # the charter-pinned canonical for the shared id
    assert name_for_id(0.0) == "PARKED"
    assert name_for_id(1.0 + 5e-7) == "SEARCH_MARKER"    # float32 round-trip jitter within tol
    assert name_for_id(-1.0) == UNMAPPED             # recorder sentinel = data bug, NOT a state
    assert name_for_id(0.5) == UNMAPPED
    assert name_for_id(float("nan")) == UNMAPPED
    assert name_for_id(None) == UNMAPPED
    # string path -- the SEARCH alias collapses; unknown junk surfaces as UNMAPPED
    assert name_for_string("SEARCH") == "SEARCH_MARKER"
    assert name_for_string("SEARCH_MARKER") == "SEARCH_MARKER"
    assert name_for_string("SEARCHING") == "SEARCHING"
    assert name_for_string(" TRACK ") == "TRACK"
    assert name_for_string("S_TRACK") == UNMAPPED    # logging the VARIABLE name is a bug
    assert name_for_string(None) == UNMAPPED
    # counters -- observed-only keys, aliases merged into the canonical bucket
    assert count_ids([3.0, 3.0, 1.0, 1.5, -1.0]) == {
        "TRACK": 2, "SEARCH_MARKER": 1, "SEARCHING": 1, UNMAPPED: 1}
    assert count_strings(["TRACK", "SEARCH", "SEARCH_MARKER", "wat"]) == {
        "TRACK": 1, "SEARCH_MARKER": 2, UNMAPPED: 1}
    assert count_strings([]) == {}
    # the module surface the other scaffolds bind to
    assert set(CANONICAL_NAMES) == {"TRACK", "REACQUIRE", "SEARCHING", "SEARCH_MARKER", "PARKED"}
    assert "SEARCH" not in CANONICAL_NAMES and "SEARCH" in NAME_TO_ID
    assert VERSION >= 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "selftest":
        raise SystemExit("usage: python fsm_groups.py selftest")
    _selftest()
    print("FSM-GROUPS-SELFTEST-OK")
