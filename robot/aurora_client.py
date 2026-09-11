#!/usr/bin/env python3
"""Fail-closed background reader for Aurora 6DOF pose in the MAP frame.

This is the foundation every map-integration path needs: it makes the Aurora's in-map pose available
to the follow node the same way perception.latest_odom() makes odometry available -- one locked
snapshot, and latest() returns None the instant the pose can't be trusted. Callers MUST treat None
as "position unknown" and fail closed; nothing may drive on a None pose.

WHY A DAEMON THREAD. The SDK's pose getter can block on a slow/dropped link, and the follow control
loop is already over budget (p99 ~240 ms). Polling it inline could stall the loop straight into the
C++ watchdog. So the SDK lives entirely on its own thread; the control loop only ever reads a lock-
protected snapshot, which can never block.

WHY WE STAMP LOCALLY. The council flagged that the SDK's cached getter can hand back a pose that is
already old behind a "fresh" call. latest() ages the pose against time.monotonic() taken AT RECEIPT
on this thread, not against any device timestamp, so a frozen stream goes stale on schedule.

MAP FRAME ONLY. start() runs require_relocalization() first and only publishes pose after RELOC-OK.
If relocalization is lost, status degrades and latest() returns None -- absolute position in the
map is exactly what a stale/lost reloc no longer provides.

Standalone self-check (needs the device connected + a map already loaded on it):
    python3 aurora_client.py [connection_string]
"""
import threading
import time


class AuroraClient:
    def __init__(self, connection_string=None, max_age=0.25, require_reloc=True):
        self.cs = connection_string
        self.max_age = max_age
        self.require_reloc = require_reloc
        self._lock = threading.Lock()
        self._pose = None          # (x, y, z, qx, qy, qz, qw) in the map frame, or None
        self._stamp = 0.0          # time.monotonic() at receipt on the reader thread
        self._status = "init"      # init | connecting | reloc | ok | lost | error
        self._stop = threading.Event()
        self._thr = None
        self._sdk = None

    # --- reader thread -------------------------------------------------------------------------
    def _run(self):
        import slamtec_aurora_sdk as sdk
        try:
            self._sdk = sdk.AuroraSDK()
            self._set_status("connecting")
            dev = None
            try:
                found = self._sdk.discover_devices(timeout=5.0)
                if found:
                    dev = found[0]
            except Exception:
                pass
            if dev is not None:
                self._sdk.connect(device_info=dev)
            else:
                self._sdk.connect(connection_string=self.cs or "192.168.127.10")
        except Exception as e:
            self._set_status("error"); self._err = repr(e); return

        if self.require_reloc:
            self._set_status("reloc")
            try:
                self._sdk.require_relocalization(timeout_ms=15000)
            except Exception:
                # Not fatal to the reader: pose may still stream, but callers keep getting None
                # until we see a good pose -- we simply never flip to "ok" without one.
                pass

        while not self._stop.is_set():
            try:
                p = self._sdk.get_current_pose(use_se3=True)
                now = time.monotonic()
                with self._lock:
                    self._pose = p
                    self._stamp = now
                    self._status = "ok"
            except Exception:
                with self._lock:
                    self._status = "lost"
            time.sleep(0.02)      # ~50 Hz poll; the loop reads the snapshot, never this call
        try:
            self._sdk.disconnect()
        except Exception:
            pass

    def _set_status(self, s):
        with self._lock:
            self._status = s

    # --- public API ----------------------------------------------------------------------------
    def start(self):
        if self._thr is None:
            self._thr = threading.Thread(target=self._run, name="aurora", daemon=True)
            self._thr.start()
        return self

    def latest(self, max_age=None):
        """Map-frame pose if it is FRESH and tracking is OK, else None. None == unknown == fail
        closed. Never raises."""
        ma = self.max_age if max_age is None else max_age
        with self._lock:
            if self._pose is None or self._status != "ok":
                return None
            if (time.monotonic() - self._stamp) > ma:
                return None
            return self._pose

    def status(self):
        with self._lock:
            age = time.monotonic() - self._stamp if self._stamp else None
            return self._status, age

    def stop(self):
        self._stop.set()
        if self._thr is not None:
            self._thr.join(timeout=2.0)


def _selfcheck(cs):
    c = AuroraClient(cs).start()
    print("started; sampling for 6 s (needs a map loaded on the device)")
    t0 = time.monotonic(); n = 0
    while time.monotonic() - t0 < 6.0:
        p = c.latest()
        st, age = c.status()
        if p is not None:
            n += 1
            if n == 1 or n % 50 == 0:
                print("  pose ok (status=%s age=%.3f): %s" % (st, age or -1, p))
        time.sleep(0.02)
    st, age = c.status()
    print("final status=%s  good-pose samples=%d" % (st, n))
    print("SELFCHECK-OK" if n > 0 else "SELFCHECK-NO-POSE (link/reloc/map issue -- fail-closed as designed)")
    c.stop()


if __name__ == "__main__":
    import sys
    _selfcheck(sys.argv[1] if len(sys.argv) > 1 else None)
