"""Rerun observability sink glue (P3.4): the crash-safe RerunSink (from k1_rerun) with a permanent
no-op fallback, plus init_rerun() which flips the module _RR on for --rerun. DEFAULT-OFF and fully
inert (byte-identical) when off. IMPORTANT: consumers must reference rerun_sink._RR (module
attribute), NOT `from rerun_sink import _RR` -- init_rerun REBINDS _RR, so a snapshot would go stale.
"""
import os
import time

from common import log

try:
    from k1_rerun import RerunSink as _RerunSink
    _RR = _RerunSink()                 # disabled-by-default -> _RR.ok is False, every method no-ops
except Exception:                      # noqa: BLE001 -- a missing sink module must never stop the node
    _RerunSink = None

    class _NullRR:                     # permanent no-op stand-in (k1_rerun.py not importable)
        ok = False

        def __getattr__(self, _n):
            return lambda *a, **k: None

    _RR = _NullRR()


def init_rerun(args):
    """Flip the module _RR sink on when --rerun is passed (Phase 2). Best-effort: a missing
    k1_rerun.py or rerun-sdk logs a warning and the follow proceeds with Rerun disabled -- Rerun
    is NEVER a safety dependency (same contract as the OSNet histogram fallback)."""
    global _RR
    if not getattr(args, "rerun", False):
        return
    if _RerunSink is None:
        log("RERUN unavailable (k1_rerun.py not importable) -> disabled (follow proceeds)")
        return
    path = None
    if args.rerun_mode == "save":
        d = args.rerun_dir or "/home/booster/rerun"
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            log("RERUN mkdir %s failed: %s -> disabled" % (d, e))
            return
        path = os.path.join(d, "k1_follow_%d.rrd" % int(time.time()))
    _RR = _RerunSink(
        enabled=True, mode=args.rerun_mode, path=path, addr=args.rerun_addr,
        image_every_n=args.rerun_image_every_n,
        min_safe=args.min_safe_range, standoff=args.standoff_m, max_follow=args.max_follow_range,
        never_disable=(getattr(args, "rerun_never_shed", "on") == "on"))
    _RR.refs_once()
    if _RR.ok:
        log("RERUN active mode=%s -> %s (image 1/%d)"
            % (args.rerun_mode, _RR.path, args.rerun_image_every_n))
        if args.drive:
            log("RERUN + --drive: ensure the on-Orin loop-cost gate PASSED (RERUN_PLAN.md); "
                "an over-budget streak auto-disables Rerun (RERUN-DISABLED-SLOW).")
    else:
        log("RERUN init failed -> disabled (follow proceeds)")
