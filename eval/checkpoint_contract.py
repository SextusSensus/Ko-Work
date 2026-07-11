#!/usr/bin/env python3
"""checkpoint_contract.py -- the FROZEN P7.2/P7.3/P7.4 checkpoint payload contract
(charter item S5 / DEFER row, docs/SCAFFOLD_CHARTER.md; full spec in docs/TRAIN_CONTRACT.md).

STDLIB ONLY, TORCH-AGNOSTIC ON PURPOSE: this module never imports torch (or numpy). The
`state_dict` value is only checked to be a dict -- at runtime it holds torch tensors, but the
contract must be importable anywhere: the laptop has no torch at all, and the P7.4 Jetson
shadow node must not drag desktop deps onto the robot just to validate a payload.

WHO IMPORTS THIS (all three -- that is the point): the future desktop `train.py` (P7.2)
builds its save payload with build_payload(); the P7.3 ONNX export and the P7.4 Jetson
shadow node both call validate_payload(payload, expect_stats_hash=<dataset card hash>)
BEFORE touching the weights. Because all three share this ONE module, a checkpoint trained
against one dataset's norm stats can never be silently paired with another's -- the #1
"great in training, dead on the robot" bug (norm mismatch) becomes structurally impossible
rather than procedurally avoided.

stats_hash definition (MUST match eval/batch_ingest.py): the sha256 hex digest of the EXACT
FILE BYTES of the dataset's meta/stats.json as written by the mint (json.dump with
sort_keys=True, indent=2). It is never recomputed from a parsed dict -- see sha256_file().

Self-test (no torch, no data, no GPU):
  python eval/checkpoint_contract.py selftest   -> prints CKPT-CONTRACT-SELFTEST-OK
"""
import hashlib
import json
import os
import sys
import tempfile

# Required top-level payload keys -> required Python type. FROZEN: adding a NEW key later is
# backward-compatible (validators ignore extras); removing or renaming one is a contract
# break and needs a docs/TRAIN_CONTRACT.md version bump.
REQUIRED_KEYS = {
    "state_dict": dict,      # model weights (torch tensors at runtime; only dict-ness here)
    "norm_stats": dict,      # PARSED meta/stats.json, verbatim (json.load of the hashed file)
    "stats_hash": str,       # sha256 hex of the exact meta/stats.json FILE BYTES (see above)
    "dataset_version": str,  # e.g. "k1_follow_v1" -- consumers refuse on mismatch
    "splits_source": str,    # identity of the splits.json consumed (read-never-resplit)
    "config": dict,          # full training config, incl. the MUST-DECIDE fields below
    "git_sha": str,          # repo SHA of the training code, or the honest string "nogit"
    "torch_version": str,    # e.g. "2.12.1+cu126"
    "env": dict,             # e.g. {"python": "...", "platform": "...", "cuda": "..."}
}

# MUST-DECIDE-BEFORE-P7.2/P7.3 config fields (docs/TRAIN_CONTRACT.md section 5). PRESENCE is
# enforced here so no checkpoint can ship without recording the decisions; the VALUES are the
# desktop's call and deliberately unconstrained.
CONFIG_MUST_DECIDE = ("chunk_size", "chunk_consumption", "obs_action_pairing", "image_norm")

_HEX = set("0123456789abcdef")


def sha256_file(path):
    """sha256 hex digest of the EXACT bytes of a file.

    This IS the stats_hash definition: stats_hash = sha256_file("<dataset>/meta/stats.json"),
    over the bytes eval/batch_ingest.py wrote (json.dump(sort_keys=True, indent=2)).
    Consumers compare hash STRINGS (checkpoint vs dataset card vs on-disk file); nobody ever
    re-serializes the parsed norm_stats dict to recompute it -- key order and float
    formatting would silently diverge and defeat the whole gate.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_payload(state_dict, norm_stats, stats_hash, dataset_version, splits_source,
                  config, git_sha, torch_version, env):
    """Assemble the checkpoint payload dict, enforcing the contract at BUILD time so a bad
    payload dies inside train.py, not weeks later on the Jetson. The caller torch.save()s
    the returned dict as-is; everything except state_dict must stay JSON-serializable."""
    payload = {
        "state_dict": state_dict, "norm_stats": norm_stats, "stats_hash": stats_hash,
        "dataset_version": dataset_version, "splits_source": splits_source,
        "config": config, "git_sha": git_sha, "torch_version": torch_version, "env": env,
    }
    validate_payload(payload)
    return payload


def validate_payload(payload, expect_stats_hash=None):
    """Raise ValueError (LOUD, names the offender) unless `payload` satisfies the contract.

    expect_stats_hash: P7.3 export and the P7.4 shadow node pass the dataset card's
    stats_hash here, asserting the checkpoint was trained against THAT dataset's norm stats.
    This string comparison is the norm-mismatch gate.
    """
    if not isinstance(payload, dict):
        raise ValueError("CKPT-CONTRACT VIOLATION: payload is %s, not a dict"
                         % type(payload).__name__)
    for key, typ in REQUIRED_KEYS.items():
        if key not in payload:
            raise ValueError("CKPT-CONTRACT VIOLATION: required key '%s' MISSING from the "
                             "checkpoint payload -- refusing. See docs/TRAIN_CONTRACT.md" % key)
        if not isinstance(payload[key], typ):
            raise ValueError("CKPT-CONTRACT VIOLATION: key '%s' must be %s, got %s"
                             % (key, typ.__name__, type(payload[key]).__name__))
    h = payload["stats_hash"]
    if len(h) != 64 or not set(h) <= _HEX:
        raise ValueError("CKPT-CONTRACT VIOLATION: stats_hash %r is not a 64-char lowercase "
                         "sha256 hex digest" % (h,))
    for key in CONFIG_MUST_DECIDE:
        if key not in payload["config"]:
            raise ValueError("CKPT-CONTRACT VIOLATION: config is missing MUST-DECIDE field "
                             "'%s' (docs/TRAIN_CONTRACT.md section 5) -- decide it BEFORE "
                             "training and record it in every checkpoint" % key)
    if expect_stats_hash is not None and h != expect_stats_hash:
        raise ValueError(
            "CKPT-CONTRACT VIOLATION: NORM-STATS MISMATCH -- checkpoint stats_hash %s != "
            "expected %s (the dataset card's). This checkpoint was trained against DIFFERENT "
            "norm stats than the dataset/deployment you are pairing it with. Refusing: it "
            "would z-score with the wrong mean/std and fail SILENTLY on the robot."
            % (h, expect_stats_hash))


# ---- selftest ----------------------------------------------------------------
def _expect_violation(fn, label):
    """Assert fn() raises the LOUD contract ValueError. A quiet failure mode here would mean
    the gate everyone relies on is decorative."""
    try:
        fn()
    except ValueError as e:
        if "CKPT-CONTRACT VIOLATION" not in str(e):
            raise AssertionError("selftest: %s raised without the LOUD prefix: %s" % (label, e))
        return
    raise AssertionError("selftest: %s did NOT raise ValueError -- contract not enforcing"
                         % label)


def _selftest():
    import copy
    import shutil
    tmp = tempfile.mkdtemp(prefix="ckpt_contract_selftest_")
    try:
        # (1) stats_hash definition: the pinned writer reproduces identical bytes -> identical
        # hash. Shape below is ILLUSTRATIVE only -- the authoritative stats.json schema is
        # minted by eval/batch_ingest.py; this module hashes BYTES, never interprets the dict.
        stats = {
            "observation.state": {"mean": [1.5, -3.0], "std": [0.5, 2.0], "count": [10]},
            "action": {"mean": [0.1, 0.0], "std": [0.2, 0.4], "count": [10]},
            "normalize": {"note": "illustrative -- batch_ingest.py owns the real schema"},
        }
        p1, p2 = (os.path.join(tmp, n) for n in ("stats.json", "stats_again.json"))
        for p in (p1, p2):
            with open(p, "w") as f:
                json.dump(stats, f, sort_keys=True, indent=2)
        h = sha256_file(p1)
        if h != sha256_file(p2):
            raise AssertionError("selftest: pinned json writer did not reproduce identical bytes")

        # (2) happy path: build -> json round-trip is byte-stable -> validates against the hash.
        # state_dict uses a JSON-able stand-in (torch tensors at runtime; module is torch-blind).
        with open(p1) as f:
            norm_stats = json.load(f)
        payload = build_payload(
            state_dict={"actor.weight": [[0.0, 1.0], [2.0, 3.0]]},
            norm_stats=norm_stats,
            stats_hash=h,
            dataset_version="k1_follow_v0_selftest",
            splits_source="k1_follow_v0_selftest/meta/splits.json",
            config={"chunk_size": 20, "chunk_consumption": "replan_every_tick",
                    "obs_action_pairing": "same_tick", "image_norm": {"scale": 255.0},
                    "lr": 0.0001},
            git_sha="nogit",
            torch_version="0.0.0-selftest",
            env={"python": "selftest"},
        )
        s1 = json.dumps(payload, sort_keys=True)
        rt = json.loads(s1)
        if json.dumps(rt, sort_keys=True) != s1:
            raise AssertionError("selftest: payload not byte-stable through a json round-trip")
        validate_payload(rt, expect_stats_hash=h)  # must NOT raise

        # (3) every guarded failure mode raises LOUDLY.
        bad = copy.deepcopy(rt); del bad["norm_stats"]
        _expect_violation(lambda: validate_payload(bad), "missing norm_stats")
        bad = copy.deepcopy(rt); bad["stats_hash"] = "deadbeef"
        _expect_violation(lambda: validate_payload(bad), "malformed stats_hash")
        _expect_violation(lambda: validate_payload(rt, expect_stats_hash="0" * 64),
                          "stats-hash mismatch")
        bad = copy.deepcopy(rt); del bad["config"]["chunk_consumption"]
        _expect_violation(lambda: validate_payload(bad), "missing MUST-DECIDE config field")
        bad = copy.deepcopy(rt); bad["config"] = "not-a-dict"
        _expect_violation(lambda: validate_payload(bad), "wrong-typed config")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("CKPT-CONTRACT-SELFTEST-OK")
    return 0


def main(argv):
    if len(argv) == 2 and argv[1] == "selftest":
        return _selftest()
    print("usage: python eval/checkpoint_contract.py selftest", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
