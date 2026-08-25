#!/usr/bin/env python3
"""Render the 3D execution web headlessly, then measure how cluttered it is.

Why this exists: the visual had to be tuned for legibility, and this machine has
no node, no headless browser and no image library. Judging "is it too busy?" by
reloading a page and squinting is slow and unrepeatable, so instead:

  dashboard/preview.js   runs the REAL renderer in JavaScriptCore against a
                         recording 2D context and dumps every draw call
  this file              rasterises those calls to a PNG and scores them

The numbers matter more than the picture. "Busy" is not a matter of taste once
you count it: what fraction of the frame is ink, how many node blobs overlap,
how much of it is covered by labels, how many edges cross. Those move in the
right direction or the change is not an improvement.

Usage:
  python3 deploy/preview_trace.py                       # mid-run, default size
  python3 deploy/preview_trace.py --scenario done --out /tmp/x.png
  python3 deploy/preview_trace.py --compare before.json  # score delta
"""

import argparse
import json
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from farm import topology as topology_mod  # noqa: E402


# --------------------------------------------------------------------- colours

def _hsl_to_rgb(h, s, light):
    h = (h % 360) / 360.0
    c = (1 - abs(2 * light - 1)) * s
    x = c * (1 - abs((h * 6) % 2 - 1))
    m = light - c / 2
    sector = int(h * 6) % 6
    r, g, b = [(c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)][sector]
    return r + m, g + m, b + m


def parse_color(value):
    """Any CSS colour the renderer actually emits -> (r, g, b, a) in 0..1."""
    if not isinstance(value, str):
        return (1.0, 1.0, 1.0, 1.0)
    text = value.strip().lower()
    m = re.match(r"hsla?\(\s*([-\d.]+)\s*,\s*([\d.]+)%\s*,\s*([\d.]+)%\s*(?:,\s*([\d.]+)\s*)?\)", text)
    if m:
        h, s, light = float(m.group(1)), float(m.group(2)) / 100, float(m.group(3)) / 100
        alpha = float(m.group(4)) if m.group(4) is not None else 1.0
        r, g, b = _hsl_to_rgb(h, s, light)
        return (r, g, b, alpha)
    m = re.match(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)", text)
    if m:
        alpha = float(m.group(4)) if m.group(4) is not None else 1.0
        return (float(m.group(1)) / 255, float(m.group(2)) / 255, float(m.group(3)) / 255, alpha)
    m = re.match(r"#([0-9a-f]{6})$", text)
    if m:
        v = int(m.group(1), 16)
        return ((v >> 16 & 255) / 255, (v >> 8 & 255) / 255, (v & 255) / 255, 1.0)
    m = re.match(r"#([0-9a-f]{3})$", text)
    if m:
        v = m.group(1)
        return (int(v[0], 16) / 15, int(v[1], 16) / 15, int(v[2], 16) / 15, 1.0)
    return (1.0, 1.0, 1.0, 1.0)


# --------------------------------------------------------------------- canvas

class Surface:
    """A float RGB buffer with the two blend modes the renderer uses."""

    def __init__(self, width, height):
        self.w, self.h = width, height
        self.buf = [0.0] * (width * height * 3)

    def blend(self, x, y, rgba, additive):
        if x < 0 or y < 0 or x >= self.w or y >= self.h:
            return
        r, g, b, a = rgba
        if a <= 0:
            return
        i = (y * self.w + x) * 3
        buf = self.buf
        if additive:
            buf[i] += r * a
            buf[i + 1] += g * a
            buf[i + 2] += b * a
        else:
            buf[i] = buf[i] * (1 - a) + r * a
            buf[i + 1] = buf[i + 1] * (1 - a) + g * a
            buf[i + 2] = buf[i + 2] * (1 - a) + b * a

    def disk(self, cx, cy, radius, rgba, additive, feather=0.7):
        if radius <= 0:
            return
        x0, x1 = int(cx - radius - 1), int(cx + radius + 2)
        y0, y1 = int(cy - radius - 1), int(cy + radius + 2)
        r, g, b, a = rgba
        for y in range(max(0, y0), min(self.h, y1)):
            dy = y + 0.5 - cy
            for x in range(max(0, x0), min(self.w, x1)):
                dx = x + 0.5 - cx
                d = math.hypot(dx, dy)
                if d > radius + feather:
                    continue
                cover = 1.0 if d <= radius - feather else (radius + feather - d) / (2 * feather)
                self.blend(x, y, (r, g, b, a * max(0.0, min(1.0, cover))), additive)

    def line(self, x0, y0, x1, y1, width, rgba, additive):
        length = math.hypot(x1 - x0, y1 - y0)
        steps = max(1, int(length))
        radius = max(0.35, width / 2)
        for i in range(steps + 1):
            t = i / steps
            self.disk(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, radius, rgba, additive, feather=0.6)

    def to_png(self, path, exposure=1.0):
        rows = bytearray()
        for y in range(self.h):
            rows.append(0)                      # PNG filter: none
            base = y * self.w * 3
            for i in range(base, base + self.w * 3):
                v = self.buf[i] * exposure
                # Mild filmic shoulder: additive glows overshoot 1.0 constantly and
                # hard clipping turns every bright cluster into one white mass,
                # which would hide exactly the crowding this is meant to measure.
                v = v / (1 + v) * 1.35 if v > 0.6 else v
                rows.append(max(0, min(255, int(v * 255 + 0.5))))

        def chunk(tag, data):
            out = struct.pack(">I", len(data)) + tag + data
            return out + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        png = b"\x89PNG\r\n\x1a\n"
        png += chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
        png += chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        png += chunk(b"IEND", b"")
        with open(path, "wb") as handle:
            handle.write(png)
        return len(png)


def gradient_color(gradient, t):
    stops = gradient.get("stops") or []
    if not stops:
        return (0, 0, 0, 0)
    t = max(0.0, min(1.0, t))
    prev = stops[0]
    for stop in stops:
        if stop[0] >= t:
            a0, c0 = prev[0], parse_color(prev[1])
            a1, c1 = stop[0], parse_color(stop[1])
            span = (a1 - a0) or 1
            k = (t - a0) / span
            return tuple(c0[i] + (c1[i] - c0[i]) * k for i in range(4))
        prev = stop
    return parse_color(stops[-1][1])


def flatten_path(path):
    """Draw commands -> polylines and circles, in screen space."""
    polys, circles, current = [], [], []
    for cmd in path:
        kind = cmd[0]
        if kind == "M":
            if len(current) > 1:
                polys.append(current)
            current = [(cmd[1], cmd[2])]
        elif kind == "L":
            current.append((cmd[1], cmd[2]))
        elif kind == "Q":
            if not current:
                current = [(cmd[3], cmd[4])]
            else:
                x0, y0 = current[-1]
                cx, cy, x1, y1 = cmd[1], cmd[2], cmd[3], cmd[4]
                for i in range(1, 13):
                    t = i / 12
                    mt = 1 - t
                    current.append((mt * mt * x0 + 2 * mt * t * cx + t * t * x1,
                                    mt * mt * y0 + 2 * mt * t * cy + t * t * y1))
        elif kind == "A":
            circles.append((cmd[1], cmd[2], cmd[3]))
    if len(current) > 1:
        polys.append(current)
    return polys, circles


def rasterise(frame, surface):
    """Replay captured draw calls. Returns the label boxes for scoring."""
    labels = []
    for op in frame["ops"]:
        kind = op["op"]
        st = op.get("st") or {}
        additive = st.get("composite") == "lighter"
        alpha = st.get("alpha", 1.0)
        if kind == "rect":
            x, y, w, h = op["x"], op["y"], op["w"], op["h"]
            grad = op.get("gradient")
            if grad and grad.get("kind") == "radial":
                cx, cy, r1 = grad["x1"], grad["y1"], max(1e-6, grad["r1"])
                for py in range(max(0, int(y)), min(surface.h, int(y + h))):
                    for px in range(max(0, int(x)), min(surface.w, int(x + w))):
                        c = gradient_color(grad, math.hypot(px + 0.5 - cx, py + 0.5 - cy) / r1)
                        surface.blend(px, py, (c[0], c[1], c[2], c[3] * alpha), additive)
            else:
                c = parse_color(op.get("color"))
                rgba = (c[0], c[1], c[2], c[3] * alpha)
                if h <= 18 and w > 8:                     # a label plate
                    labels.append({"x": x, "y": y, "w": w, "h": h})
                for py in range(max(0, int(y)), min(surface.h, int(y + h + 0.5))):
                    for px in range(max(0, int(x)), min(surface.w, int(x + w + 0.5))):
                        surface.blend(px, py, rgba, additive)
        elif kind == "sprite":
            stops = op.get("stops")
            x, y, w, h = op["x"], op["y"], op["w"], op["h"]
            cx, cy, radius = x + w / 2, y + h / 2, max(w, h) / 2
            if not stops or radius <= 0:
                continue
            grad = {"stops": stops}
            x0, x1 = int(cx - radius - 1), int(cx + radius + 2)
            y0, y1 = int(cy - radius - 1), int(cy + radius + 2)
            for py in range(max(0, y0), min(surface.h, y1)):
                for px in range(max(0, x0), min(surface.w, x1)):
                    d = math.hypot(px + 0.5 - cx, py + 0.5 - cy) / radius
                    if d > 1:
                        continue
                    c = gradient_color(grad, d)
                    surface.blend(px, py, (c[0], c[1], c[2], c[3] * alpha), additive)
        elif kind in ("stroke", "fill"):
            color = parse_color(st.get("stroke") if kind == "stroke" else st.get("fill"))
            rgba = (color[0], color[1], color[2], color[3] * alpha)
            polys, circles = flatten_path(op["path"])
            width = st.get("lineWidth", 1)
            for poly in polys:
                for i in range(len(poly) - 1):
                    surface.line(poly[i][0], poly[i][1], poly[i + 1][0], poly[i + 1][1],
                                 width, rgba, additive)
            for (cx, cy, r) in circles:
                if kind == "fill":
                    surface.disk(cx, cy, r, rgba, additive)
                else:
                    steps = max(12, int(r * 3))
                    pts = [(cx + math.cos(i / steps * math.tau) * r,
                            cy + math.sin(i / steps * math.tau) * r) for i in range(steps + 1)]
                    for i in range(len(pts) - 1):
                        surface.line(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1],
                                     width, rgba, additive)
        elif kind == "text":
            # Glyphs are not reproduced; the plate already occupies the space, and
            # a bar of ink at the right width is what matters for crowding. The
            # actual strings are reported separately.
            width = len(op["text"]) * 6.05
            c = parse_color(st.get("fill"))
            for py in range(int(op["y"] - 4), int(op["y"] + 4)):
                for px in range(int(op["x"]), int(op["x"] + width)):
                    if (px + py) % 3:                    # dotted: reads as text, not a slab
                        surface.blend(px, py, (c[0], c[1], c[2], c[3] * alpha * 0.75), additive)
            labels.append({"x": op["x"], "y": op["y"] - 7, "w": width, "h": 15, "text": op["text"]})
    return labels


# --------------------------------------------------------------------- metrics

def score(frame, surface, labels):
    w, h = surface.w, surface.h
    pixels = w * h
    buf = surface.buf
    lum = [0.0] * pixels
    for i in range(pixels):
        lum[i] = 0.2126 * buf[i * 3] + 0.7152 * buf[i * 3 + 1] + 0.0722 * buf[i * 3 + 2]
    floor = sorted(lum)[pixels // 20]                 # 5th percentile = backdrop
    ink = sum(1 for v in lum if v > floor + 0.05)
    hot = sum(1 for v in lum if v > 0.45)
    blown = sum(1 for v in lum if v > 0.92)

    shown = [n for n in frame["nodes"] if n["visible"] and n["reveal"] > 0.25]
    pairs = overlaps = 0
    nearest = []
    for i, a in enumerate(shown):
        best = 1e9
        for j, b in enumerate(shown):
            if i == j:
                continue
            d = math.hypot(a["x"] - b["x"], a["y"] - b["y"])
            best = min(best, d)
            if j > i:
                pairs += 1
                if d < (a["r"] + b["r"]) * 1.6:       # visible blobs merge
                    overlaps += 1
        if best < 1e9:
            nearest.append(best)
    nearest.sort()

    text_labels = [l for l in labels if "text" in l]
    label_area = sum(l["w"] * l["h"] for l in text_labels)
    collisions = 0
    for i, a in enumerate(text_labels):
        for b in text_labels[i + 1:]:
            if (a["x"] < b["x"] + b["w"] and a["x"] + a["w"] > b["x"]
                    and a["y"] < b["y"] + b["h"] and a["y"] + a["h"] > b["y"]):
                collisions += 1

    # Edge crossings: a proxy for "is this a diagram or a hairball?" Measured on
    # the flattened curves, not on straight chords between endpoints - bundling
    # changes only the curve, so chord crossings would score it as no change.
    segs = []
    for op in frame["ops"]:
        st = op.get("st") or {}
        # Effective opacity is globalAlpha * the alpha inside the stroke colour.
        # The renderer puts edge opacity in the colour, so reading globalAlpha
        # alone scored every edge as fully opaque and hid the length fade.
        alpha = st.get("alpha", 1) * parse_color(st.get("stroke"))[3]
        if op["op"] != "stroke" or alpha < 0.03:
            continue
        polys, _ = flatten_path(op["path"])
        for poly in polys:
            stride = max(1, (len(poly) - 1) // 4)          # ~4 segments per curve
            for i in range(0, len(poly) - 1, stride):
                j = min(len(poly) - 1, i + stride)
                segs.append((poly[i], poly[j], alpha))
    crossings = 0
    visible_crossings = 0

    def side(o, a, b):
        return (b[0] - a[0]) * (o[1] - a[1]) - (b[1] - a[1]) * (o[0] - a[0])

    for i in range(len(segs)):
        a0, a1, aa = segs[i]
        for j in range(i + 1, len(segs)):
            b0, b1, ba = segs[j]
            d1, d2 = side(b0, a0, a1), side(b1, a0, a1)
            d3, d4 = side(a0, b0, b1), side(a1, b0, b1)
            if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
                crossings += 1
                # A crossing you cannot see is not clutter. This is the number that
                # tracks how tangled the picture actually looks.
                if aa >= 0.1 and ba >= 0.1:
                    visible_crossings += 1

    near_half = sorted(shown, key=lambda n: n["depth"])
    half = max(1, len(near_half) // 2)
    return {
        "ink_pct": round(100 * ink / pixels, 2),
        "hot_pct": round(100 * hot / pixels, 2),
        "blown_pct": round(100 * blown / pixels, 3),
        "nodes_shown": len(shown),
        "blob_overlap_pct": round(100 * overlaps / max(1, pairs), 2),
        "nearest_px_p10": round(nearest[len(nearest) // 10], 1) if nearest else 0,
        "nearest_px_median": round(nearest[len(nearest) // 2], 1) if nearest else 0,
        "labels": len(text_labels),
        "label_area_pct": round(100 * label_area / pixels, 2),
        "label_collisions": collisions,
        "edge_crossings": crossings,
        "crossings_visible": visible_crossings,
        "edge_segments": len(segs),
        "near_scale": round(sum(n["scale"] for n in near_half[:half]) / half, 3),
        "far_scale": round(sum(n["scale"] for n in near_half[-half:]) / half, 3),
    }


# --------------------------------------------------------------------- scenarios

def pipeline_for(scenario, steps):
    names = [s["name"] for s in steps]
    done_through = {"early": 3, "mid": 8, "done": len(names)}.get(scenario, 8)
    rows, base = [], 1755790000
    for index, name in enumerate(names):
        if index < done_through - 1:
            status = "skipped" if name in ("adopt", "expand") and index % 3 == 0 else "done"
        elif index == done_through - 1 and scenario != "done":
            status = "active"
        else:
            status = "pending"
        row = {"name": name, "status": status,
               "started_at": base + index * 6 if status != "pending" else None,
               "seconds": round(1.2 + (index % 5) * 0.8, 2) if status == "done" else None}
        rows.append(row)
    return {"steps": rows, "run": 217, "started_at": base}


def activity_for(graph, count=6):
    tools = [n["label"] for n in graph["nodes"] if n["kind"] == "tool"]
    return [{"tool": tools[i % len(tools)], "step": None, "ok": True} for i in range(count)]


def capture(args, graph):
    payload = {
        "topology": graph,
        "pipeline": pipeline_for(args.scenario, graph["steps"]),
        "activity": activity_for(graph),
        "width": args.width, "height": args.height,
        "seconds": args.seconds, "settle": args.settle,
        "instant": args.instant, "highlight": args.highlight, "quality": 1,
        "focus": args.focus,
        "tuning": json.loads(args.tuning) if args.tuning else None,
    }
    with tempfile.TemporaryDirectory() as tmp:
        in_path = os.path.join(tmp, "in.json")
        out_path = os.path.join(tmp, "frame.json")
        payload["out"] = out_path
        with open(in_path, "w") as handle:
            json.dump(payload, handle)
        proc = subprocess.run(["osascript", "-l", "JavaScript", "dashboard/preview.js", in_path],
                              cwd=ROOT, capture_output=True, text=True, timeout=180)
        if not os.path.exists(out_path):
            raise SystemExit("preview: capture failed\n%s\n%s" % (proc.stdout, proc.stderr))
        with open(out_path) as handle:
            return json.load(handle)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="mid", choices=("early", "mid", "done"))
    ap.add_argument("--width", type=int, default=620)
    ap.add_argument("--height", type=int, default=380)
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--settle", type=int, default=300)
    ap.add_argument("--instant", action="store_true")
    ap.add_argument("--highlight", default=None)
    ap.add_argument("--out", default="/tmp/trace_preview/frame.png")
    ap.add_argument("--save-score", default=None)
    ap.add_argument("--compare", default=None)
    ap.add_argument("--focus", action="store_true",
                    help="soft-focus the live step, which is the panel's default")
    ap.add_argument("--check", action="store_true",
                    help="fail if the frame is less legible than the agreed bounds")
    ap.add_argument("--tuning", default=None, help='JSON of Trace3D.TUNING overrides')
    ap.add_argument("--labels", action="store_true", help="print the placed label strings")
    args = ap.parse_args()

    graph = topology_mod.graph()
    frame = capture(args, graph)
    surface = Surface(frame["width"], frame["height"])
    labels = rasterise(frame, surface)
    metrics = score(frame, surface, labels)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    size = surface.to_png(args.out)

    print("%s  %dx%d  %d ops  %d/%d lit  %.1fKB png"
          % (args.scenario, frame["width"], frame["height"], len(frame["ops"]),
             frame["counts"]["lit"], frame["counts"]["nodes"], size / 1024))
    print("   %s" % args.out)
    baseline = {}
    if args.compare and os.path.exists(args.compare):
        with open(args.compare) as handle:
            baseline = json.load(handle)
    for key in sorted(metrics):
        line = "   %-18s %8s" % (key, metrics[key])
        if key in baseline:
            delta = metrics[key] - baseline[key]
            line += "   (was %s, %+.2f)" % (baseline[key], delta)
        print(line)
    if args.labels:
        print("   labels: %s" % ", ".join(sorted(l["text"] for l in labels if "text" in l)))
    if args.save_score:
        with open(args.save_score, "w") as handle:
            json.dump(metrics, handle, indent=1)

    if args.check:
        # Legibility bounds, not beauty. Each one is a regression that was real at
        # some point while building this: a frame drowning in ink, node blobs
        # merging into a haze, labels covering the graph they annotate, and an
        # edge hairball. Generous enough that ordinary tuning does not trip them.
        bounds = [
            ("ink_pct", None, 20.0),
            ("hot_pct", None, 6.0),
            ("blown_pct", None, 1.5),
            ("blob_overlap_pct", None, 3.0),
            ("nearest_px_p10", 6.0, None),
            ("label_area_pct", None, 8.0),
            ("label_collisions", None, 0),
            ("crossings_visible", None, 200),
        ]
        bad = []
        for key, low, high in bounds:
            value = metrics[key]
            if low is not None and value < low:
                bad.append("%s=%s below %s" % (key, value, low))
            if high is not None and value > high:
                bad.append("%s=%s above %s" % (key, value, high))
        if bad:
            print("   LEGIBILITY FAIL: %s" % "; ".join(bad))
            raise SystemExit(1)
        print("   legibility ok (%d bounds)" % len(bounds))


if __name__ == "__main__":
    main()
