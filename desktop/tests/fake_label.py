"""Stand-in for eval/rrd_label.py in the stage harness: same CLI surface, no GPU work.

Sleeps FAKE_LABEL_SLEEP seconds (to exercise the stage's wall-clock cap), then writes a PASS
validation at the current gate_version and a placeholder labeled.rrd where the stage asked for them.
"""
import argparse
import json
import os
import time

ap = argparse.ArgumentParser()
ap.add_argument("rrd")
ap.add_argument("--out")
ap.add_argument("--jsonl")
ap.add_argument("--summary-json")
ap.add_argument("--validation-json")
a, _unknown = ap.parse_known_args()
time.sleep(float(os.environ.get("FAKE_LABEL_SLEEP", "0")))
with open(a.validation_json, "w", encoding="utf-8") as fh:
    json.dump({"gate_version": 3, "status": "PASS",
               "checks": {"floor": {"status": "PASS"}, "scale": {"status": "PASS"},
                          "walls": {"status": "PASS"}}}, fh)
with open(a.out, "wb") as fh:
    fh.write(b"fake")
print("fake labeller done")
