"""Write the raw LiDAR points of the mesh.py window to a .glb (glTF POINTS) to look at in web/viewer.html.

    python3 points.py "data/out/09LD0853+09LD0854+09LD0863+09LD0864_cells.npz" out/vr/points.glb \
        --rows 200 488 --cols 220 476 --max 4000000

Same window and local axes as mesh.py (x east, y up, z south, origin at the window centre,
height 0 at the lowest ground), so the two files sit on top of each other in the viewer.
Points are thinned evenly to at most --max; colours are the photo RGB of each point.

Without a cells file, give the tiles and a rectangle (EPSG:6677, m) instead; height 0 is then
the lowest ground point (1st percentile of class 2, against noise):

    python3 points.py - out/ueno/points.glb --tiles 09LD0867 --bbox -5134 -32100 -4878 -31812
"""
import argparse, json, struct, sys
import numpy as np, laspy


def window(cells, rows, cols):
    """The cells file and the frame of the mesh.py window: (d, cell, x0, y1, W, H, cx, cy, base)."""
    d = np.load(cells)
    cell = float(d["cell"]) if "cell" in d else 1.0
    x0 = d["origin"][0] + cols[0] * cell          # west edge (EPSG:6677)
    y1 = d["origin"][1] - rows[0] * cell          # north edge
    W, H = cols[1] - cols[0], rows[1] - rows[0]
    cx, cy = x0 + W * cell / 2, y1 - H * cell / 2
    base = float(np.nanmin(d["ground"][rows[0]:rows[1], cols[0]:cols[1]]))
    return d, cell, x0, y1, W, H, cx, cy, base


def bbox_frame(tiles, bbox):
    """The frame of a rectangle X0 Y0 X1 Y1 without a cells file: (cell, x0, y1, W, H, cx, cy, base)."""
    x0, y0, x1, y1 = bbox
    g = []
    for t in tiles:
        las = laspy.read(f"data/lidar/{t}.las")
        x, y, k = np.asarray(las.x), np.asarray(las.y), np.asarray(las.classification)
        m = (x >= x0) & (x < x1) & (y > y0) & (y <= y1) & (k == 2)
        g.append(np.asarray(las.z)[m])
    base = float(np.percentile(np.concatenate(g), 1))
    return 1.0, x0, y1, int(x1 - x0), int(y1 - y0), (x0 + x1) / 2, (y0 + y1) / 2, base


def load(cells, rows, cols, tiles=None, bbox=None):
    """LiDAR points inside the window: local (east, north, up) from the window centre and lowest ground,
    8-bit RGB and the LAS class (2 = ground)."""
    if bbox:
        cell, x0, y1, W, H, cx, cy, base = bbox_frame(tiles, bbox)
    else:
        d, cell, x0, y1, W, H, cx, cy, base = window(cells, rows, cols)
        tiles = cells.split("/")[-1].split("_cells")[0].split("+")
    P, C, K = [], [], []
    for t in tiles:
        las = laspy.read(f"data/lidar/{t}.las")
        x, y, z = np.asarray(las.x), np.asarray(las.y), np.asarray(las.z)
        m = (x >= x0) & (x < x0 + W * cell) & (y <= y1) & (y > y1 - H * cell)
        P.append(np.column_stack([x[m] - cx, y[m] - cy, z[m] - base]))
        rgb = np.column_stack([np.asarray(las.red)[m], np.asarray(las.green)[m], np.asarray(las.blue)[m]])
        C.append((rgb >> 8 if rgb.max() > 255 else rgb).astype(np.uint8))
        K.append(np.asarray(las.classification)[m])
    return np.concatenate(P), np.concatenate(C), np.concatenate(K)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cells"); ap.add_argument("out")
    ap.add_argument("--rows", type=int, nargs=2)
    ap.add_argument("--cols", type=int, nargs=2)
    ap.add_argument("--tiles", nargs="+", help="with --bbox, instead of a cells file")
    ap.add_argument("--bbox", type=float, nargs=4, metavar=("X0", "Y0", "X1", "Y1"))
    ap.add_argument("--max", type=int, default=4_000_000)
    a = ap.parse_args()
    if a.bbox:
        cell, x0, y1, W, H, cx, cy, base = bbox_frame(a.tiles, a.bbox)
        print(f"frame: centre {cx} {cy} (EPSG:6677), base {base:.2f} m", file=sys.stderr)
    else:
        W, H = a.cols[1] - a.cols[0], a.rows[1] - a.rows[0]
        cell = window(a.cells, a.rows, a.cols)[1]
    P, C, _ = load(a.cells, a.rows, a.cols, a.tiles, a.bbox)
    P = np.column_stack([P[:, 0], P[:, 2], -P[:, 1]]).astype(np.float32)     # glTF: x east, y up, z south
    n = len(P)
    if n > a.max:
        keep = np.random.default_rng(0).choice(n, a.max, replace=False)
        P, C = P[keep], C[keep]
    lin = np.round((C / 255.0) ** 2.2 * 65535).astype(np.uint16)    # glTF colours are linear, the photo is sRGB
    col = np.column_stack([lin, np.full(len(C), 65535, np.uint16)])
    pos, colb = P.tobytes(), col.tobytes()
    gltf = {"asset": {"version": "2.0", "generator": "hongo-campus-minecraft points.py"},
            "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"name": "points", "mesh": 0}],
            "meshes": [{"name": "points", "primitives": [{"attributes": {"POSITION": 0, "COLOR_0": 1}, "mode": 0}]}],
            "accessors": [
                {"bufferView": 0, "componentType": 5126, "count": len(P), "type": "VEC3",
                 "min": P.min(0).tolist(), "max": P.max(0).tolist()},
                {"bufferView": 1, "componentType": 5123, "normalized": True, "count": len(P), "type": "VEC4"}],
            "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(pos)},
                            {"buffer": 0, "byteOffset": len(pos), "byteLength": len(colb)}],
            "buffers": [{"byteLength": len(pos) + len(colb)}]}
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    bin_ = pos + colb
    bin_ += b"\0" * (-len(bin_) % 4)
    with open(a.out, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(bin_)))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(bin_), 0x004E4942) + bin_)
    print(f"{n:,} points in the window, wrote {len(P):,} ({n / (W * H * cell * cell):.1f} per m2) to {a.out}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
