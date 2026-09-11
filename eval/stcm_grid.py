#!/usr/bin/env python3
"""Extract the 2D occupancy grid from a SLAMTEC .stcm map.

WHY THIS IS 40 LINES AND NOT A PARSER. A .stcm is a container: one small
`vnd.slamtec.map-layer/vnd.grid-map+binary` occupancy layer plus a very large visual
keyframe/descriptor database used by the Aurora itself to relocalize. Only the grid is useful to
this robot -- the keyframe DB is the device's business. Measured on the operator's map: 1.14 MB of
grid inside a 187.6 MB file. So this reads the ASCII header for the grid's shape/origin/resolution
and takes the payload as a flat uint8 block; it does not attempt to understand the rest.

  python stcm_grid.py map.stcm --npy map_grid.npy --png map_grid.png

Frames: grid x/y are METRES in the SLAM map frame, cell (col,row) -> (origin + index*resolution).
That frame is the Aurora's, NOT the robot's odom frame -- they share no origin and nothing here
relates them. Localising the robot in this grid needs live pose from the device; see DECISIONS.md.
"""
import argparse
import re
import sys

import numpy as np


def layers(b):
    """Every layer declared in the file, in offset order. Printed on every run because the FIRST
    read of this format saw only the grid layer in the first 8 KB and concluded that was the whole
    map -- it is one of five, and the 3D data is elsewhere."""
    out = []
    for m in re.finditer(rb"vnd\.slamtec\.map-layer/(vnd\.[!-~]+)", b):
        kind = m.group(1).decode(errors="replace")
        after = [t.decode() for t in re.findall(rb"[ -~]{3,}", b[m.end():m.end() + 120])]
        out.append((m.start(), kind, after[1] if len(after) > 1 else "?"))
    return sorted(set(out))


def load(path):
    """-> (grid uint8 [H,W], meta dict). Raises on anything it cannot recognise."""
    b = open(path, "rb").read()
    if b[16:20] != b"STCM":
        raise SystemExit("not a STCM file (magic at 0x10 is %r)" % b[16:20])
    print("layers in this map:")
    for off, kind, usage in layers(b):
        print("  0x%09x  %-38s usage=%s" % (off, kind, usage))
    print("  (this tool reads the grid layer only; the vslam feature map is the device's "
          "relocalization data, and a DENSE POINT CLOUD is not stored here -- it is a separate\n"
          "   export from aurora_map_densifier.exe)")
    toks = [t.decode() for t in re.findall(rb"[ -~]{3,}", b[:8192])]
    meta = {}
    for i, t in enumerate(toks):
        if t in ("dimension_width", "dimension_height", "origin_x", "origin_y",
                 "resolution_x", "resolution_y", "usage") and i + 1 < len(toks):
            meta[t] = toks[i + 1]

    def num(k, cast):
        return cast(re.sub(r"[^0-9.\-]", "", meta[k]))

    w, h = num("dimension_width", int), num("dimension_height", int)
    res = num("resolution_x", float)
    ox, oy = num("origin_x", float), num("origin_y", float)

    # Payload: the first run after the layer declaration that is dominated by a handful of byte
    # values -- an occupancy grid is, by construction, mostly unknown/free/occupied.
    # ponytail: offset-scan instead of a real container parse; if a future map fails to load,
    # decode the layer table properly rather than widening this heuristic.
    start = b.find(b"vnd.grid-map+binary")
    if start < 0:
        raise SystemExit("no grid-map layer in this file")
    n = w * h
    for off in range(start, min(start + 200_000, len(b) - n)):
        if len(np.unique(np.frombuffer(b[off:off + 2048], dtype=np.uint8))) <= 8:
            g = np.frombuffer(b[off:off + n], dtype=np.uint8).reshape(h, w)
            return g, {"w": w, "h": h, "res": res, "origin": (ox, oy), "offset": off,
                       "usage": meta.get("usage", "?")}
    raise SystemExit("found the layer declaration but no plausible grid payload after it")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stcm")
    ap.add_argument("--npy", help="write the raw uint8 grid here")
    ap.add_argument("--png", help="write a preview image here (needs PIL)")
    a = ap.parse_args()

    g, m = load(a.stcm)
    print("grid %dx%d @ %.3f m  ->  %.1f m x %.1f m   origin (%.2f, %.2f)  usage=%s"
          % (m["w"], m["h"], m["res"], m["w"] * m["res"], m["h"] * m["res"],
             m["origin"][0], m["origin"][1], m["usage"]))
    vals, counts = np.unique(g, return_counts=True)
    top = sorted(zip(counts.tolist(), vals.tolist()), reverse=True)[:4]
    print("payload at 0x%x; most common values: %s"
          % (m["offset"], ", ".join("%d=%.1f%%" % (v, 100.0 * c / g.size) for c, v in top)))

    if a.npy:
        np.save(a.npy, g)
        print("wrote", a.npy)
    if a.png:
        from PIL import Image
        # Three classes, because that is what the grid means and a ramp hid it: unknown mid-grey,
        # free light, occupied near-black. Occupied (>=128) is walls; free (127) is the walked
        # interior; 1-126 are partial/ray returns rendered a shade darker than free.
        img = np.full(g.shape, 150, dtype=np.uint8)          # unknown
        img[(g >= 1) & (g <= 126)] = 205                     # partial
        img[g == 127] = 245                                  # free
        img[g >= 128] = 25                                   # occupied (walls)
        Image.fromarray(img).save(a.png)
        print("wrote", a.png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
