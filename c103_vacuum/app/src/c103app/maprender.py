"""Turn a saved c103 map (grid + room outlines) into a base PNG plus the numbers the page needs.

The vector format (RLE grid + room_chains) is the one produced by xiaomi-vac's map_vector.py
(MIT, (c) 2026 letitbe-dull; see third_party/LICENSE-xiaomi-vac.txt). Rendering happens once at
import time; the web page then only draws the robot and its trail on top.
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageOps

from . import spec

SCALE = 3  # output pixels per grid cell (a cell is `resolution` metres, normally 0.05)
PALETTE = [(120, 200, 190), (110, 150, 205), (150, 190, 230), (245, 175, 70),
           (110, 190, 140), (200, 150, 210)]
WALL = (70, 70, 80, 255)
FLOOR = (225, 225, 230, 255)


def _decode_grid(v: dict) -> list[int]:
    grid: list[int] = []
    for val, n in zip(v["grid_rle"][0::2], v["grid_rle"][1::2]):
        grid.extend([val] * n)
    return grid + [0] * (v["size"]["x"] * v["size"]["y"] - len(grid))


def render(v: dict, floor: str) -> tuple[bytes, dict]:
    """Return (png_bytes, meta). `meta` maps robot metres to image pixels and lists the rooms."""
    w, h, res, b = v["size"]["x"], v["size"]["y"], v["resolution"], v["bounds"]
    grid = _decode_grid(v)

    # Room membership comes from the room outlines: the grid itself only says floor/wall.
    owner = Image.new("L", (w, h), 0)
    od = ImageDraw.Draw(owner)
    for chain in sorted(v["room_chains"], key=lambda c: -max(len(r) for r in c["rings"])):
        for ring in chain["rings"]:
            if len(ring) > 2:
                od.polygon([tuple(p) for p in ring], fill=chain["id"])
    own = owner.load()

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = img.load()
    for i, val in enumerate(grid):
        x, y = i % w, i // w
        rid = own[x, y]
        if val in (255, 128):
            px[x, y] = WALL
        elif rid:
            px[x, y] = PALETTE[(rid - 10) % len(PALETTE)] + (255,)
        elif val:
            px[x, y] = FLOOR

    # The saved grid starts at the SOUTH edge (row 0 = minY). Flip so north is up, like the Mi Home map.
    img = ImageOps.flip(img)
    flip = lambda r: h - 1 - r  # noqa: E731  grid row -> row in the flipped image

    pad = 6
    x0, y0, x1, y1 = img.getbbox()
    x0, y0, x1, y1 = max(x0 - pad, 0), max(y0 - pad, 0), min(x1 + pad, w), min(y1 + pad, h)
    img = img.crop((x0, y0, x1, y1)).resize(((x1 - x0) * SCALE, (y1 - y0) * SCALE), Image.NEAREST)

    # metres -> pixels: col = (x - minX)/res, row = (maxY - y)/res, minus the crop, times SCALE
    ox = ((0 - b["minX"]) / res - x0) * SCALE
    oy = ((b["maxY"] - 0) / res - y0) * SCALE
    ppm = SCALE / res
    to_px = lambda col, row: ((col - x0) * SCALE, (row - y0) * SCALE)  # noqa: E731

    on_map = {r["id"]: r["name"] for r in v.get("rooms", [])}   # names stored in the robot's own map
    sums: dict[int, list[float]] = {}
    for i in range(w * h):
        rid = own[i % w, i // w]
        if rid:
            t = sums.setdefault(rid, [0, 0, 0])
            t[0] += i % w
            t[1] += i // w
            t[2] += 1
    rooms = []
    for chain in v["room_chains"]:
        rid = chain["id"]
        cx, cy = (sums[rid][0] / sums[rid][2], flip(sums[rid][1] / sums[rid][2])) if rid in sums else (0, 0)
        lx, ly = to_px(cx, cy)
        name = on_map.get(rid)
        name = None if spec.is_placeholder(name) else name     # None = the room has no name on the robot
        rooms.append({"id": rid, "name": name, "label_px": [round(lx), round(ly)],
                      "rings_px": [[[round(c), round(r)] for c, r in (to_px(p[0], flip(p[1])) for p in ring)]
                                   for ring in chain["rings"]]})

    cb = v["charger"]
    meta = {"map_id": v["map_id"], "size_px": list(img.size), "origin_px": [ox, oy],
            "px_per_m": ppm, "charger": cb, "rooms": rooms}
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), meta

