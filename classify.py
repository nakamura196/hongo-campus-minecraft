"""Split LiDAR class 1 (non-ground) into building / tree / other.

The Tokyo 23-ku point cloud only has ground (2) vs everything else (1).
We separate class 1 using three clues:
  - PLATEAU roof outlines (footprints.py output): inside = building candidate
  - colour: excess green ExG = (2G - R - B) / (R + G + B)
  - returns: a non-last return of a multi-return pulse means the laser went
    partly through something (leaves); roofs give a single return

    python classify.py TILE.las BLDG.geojson OUTDIR [--exg 0.05]

Writes OUTDIR/<tile>_labels.png (top view), <tile>_rgb.png, and
<tile>_cells.npz (1 m grid: label, top height, ground height) for the builder.
"""
import json, os, sys
import numpy as np, laspy
from PIL import Image, ImageDraw

GROUND, BUILDING, TREE, OTHER, WATER, LOW = 0, 1, 2, 3, 4, 5
COLORS = np.array([[150, 130, 100], [210, 70, 60], [40, 150, 50],
                   [80, 120, 230], [30, 60, 160], [190, 180, 150]], np.uint8)


def fill_nan(a, iters=200):
    """Fill holes in a grid with the mean of known neighbours (no scipy here)."""
    a = a.copy()
    for _ in range(iters):
        m = np.isnan(a)
        if not m.any():
            break
        p = np.pad(a, 1, constant_values=np.nan)
        nb = np.stack([p[:-2, 1:-1], p[2:, 1:-1], p[1:-1, :-2], p[1:-1, 2:]])
        cnt = (~np.isnan(nb)).sum(0)
        s = np.nansum(nb, 0)
        upd = m & (cnt > 0)
        a[upd] = s[upd] / cnt[upd]
    return a


def main():
    args = sys.argv[1:]
    exg_t = 0.05
    if "--exg" in args:
        i = args.index("--exg"); exg_t = float(args[i + 1]); del args[i:i + 2]
    las_path, bldg_path, outdir = args
    tile = os.path.splitext(os.path.basename(las_path))[0]
    os.makedirs(outdir, exist_ok=True)

    las = laspy.read(las_path)
    x, y, z = np.asarray(las.x), np.asarray(las.y), np.asarray(las.z)
    cls = np.asarray(las.classification)
    rn, nr = np.asarray(las.return_number), np.asarray(las.number_of_returns)
    r, g, b = [np.asarray(las[k]).astype(np.float32) for k in ("red", "green", "blue")]
    x0, y1 = np.floor(x.min() / 100) * 100, np.ceil(y.max() / 100) * 100
    W, H = int(np.ceil((x.max() - x0))), int(np.ceil((y1 - y.min())))
    ix = np.clip((x - x0).astype(int), 0, W - 1)
    iy = np.clip((y1 - y).astype(int), 0, H - 1)
    k = iy * W + ix
    print(f"{tile}: {len(x):,} points, grid {W}x{H} m, origin x={x0} y={y1}")

    # ground height per 1 m cell: lowest ground return, holes filled from neighbours
    dem = np.full(W * H, np.inf)
    gm = cls == 2
    np.minimum.at(dem, k[gm], z[gm])
    dem[np.isinf(dem)] = np.nan
    dem = fill_nan(dem.reshape(H, W))
    hag = z - dem.ravel()[k]

    # building outlines rasterised at 0.25 m so edge points are judged fairly
    S = 4
    img = Image.new("1", (W * S, H * S), 0)
    dr = ImageDraw.Draw(img)
    feats = json.load(open(bldg_path, encoding="utf-8"))["features"]
    for f in feats:
        for poly in f["geometry"]["coordinates"]:
            ring = [((px - x0) * S, (y1 - py) * S) for px, py in poly[0]]
            dr.polygon(ring, fill=1)
    fine = np.array(img, bool)
    inb = fine[np.clip(((y1 - y) * S).astype(int), 0, H * S - 1),
               np.clip(((x - x0) * S).astype(int), 0, W * S - 1)]

    exg = (2 * g - r - b) / np.maximum(r + g + b, 1)
    green = exg > exg_t
    passthru = (nr > 1) & (rn < nr)
    c1 = cls == 1

    # --- diagnostics: how well do colour and returns tell roofs from trees? ---
    def share(m, name):
        print(f"  {name:28s} n={m.sum():>9,}  green {green[m].mean()*100:5.1f}%  "
              f"pass-through {passthru[m].mean()*100:5.1f}%  both {(green & passthru)[m].mean()*100:5.1f}%")
    print("class 1 points:")
    share(c1 & inb, "inside outline")
    share(c1 & inb & (hag > 3), "inside outline, >3 m")
    share(c1 & ~inb, "outside outline")
    share(c1 & ~inb & (hag > 3), "outside outline, >3 m")

    # --- point labels ---
    lab = np.full(len(x), GROUND, np.uint8)
    veg = green | passthru
    lab[c1 & (hag < 0.5)] = LOW                     # kerbs, cars' shadows, noise
    tall = c1 & (hag >= 0.5)
    lab[tall & inb] = BUILDING
    lab[tall & inb & green & passthru] = TREE       # branches over a roof
    lab[tall & ~inb & veg] = TREE
    lab[tall & ~inb & ~veg] = OTHER                 # cars, walls, sheds not in PLATEAU
    for name, v in (("building", BUILDING), ("tree", TREE), ("other", OTHER), ("low", LOW)):
        print(f"  label {name:8s} {np.mean(lab == v)*100:5.1f}% of points")

    # --- 1 m cells: label of the highest point; empty cells inside nothing = water ---
    o = np.lexsort((z, k))
    top = o[np.r_[k[o][1:] != k[o][:-1], True]]
    cell_lab = np.full(W * H, WATER, np.uint8)
    cell_top = np.full(W * H, np.nan, np.float32)
    cell_lab[k[top]] = lab[top]
    cell_top[k[top]] = z[top]
    # a cell whose top is a tree but most of its tall points are building stays building (eaves)
    nb_ = np.bincount(k, (lab == BUILDING), W * H)
    nt_ = np.bincount(k, (lab == TREE), W * H)
    cell_lab[(cell_lab == TREE) & (nb_ > 2 * nt_)] = BUILDING
    cell_lab = cell_lab.reshape(H, W)
    # dark, single-return leaves come out as OTHER; inside a canopy, call them tree
    p = np.pad(cell_lab, 1, mode="edge")
    trees = sum((p[1 + dy:H + 1 + dy, 1 + dx:W + 1 + dx] == TREE)
                for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx)
    fix = (cell_lab == OTHER) & (trees >= 5)
    cell_lab[fix] = TREE
    print(f"  {fix.sum()} OTHER cells inside canopy -> tree")
    for name, v in (("ground", GROUND), ("building", BUILDING), ("tree", TREE),
                    ("other", OTHER), ("water/empty", WATER), ("low", LOW)):
        print(f"  cells {name:11s} {np.mean(cell_lab == v)*100:5.1f}%")

    np.savez_compressed(os.path.join(outdir, f"{tile}_cells.npz"), label=cell_lab,
                        top=cell_top.reshape(H, W), ground=dem.astype(np.float32),
                        origin=np.array([x0, y1]))

    # --- pictures ---
    pic = Image.fromarray(COLORS[cell_lab])
    d = ImageDraw.Draw(pic)
    for f in feats:
        for poly in f["geometry"]["coordinates"]:
            d.line([(px - x0, y1 - py) for px, py in poly[0]], fill=(255, 255, 255), width=1)
    pic.resize((W * 2, H * 2), Image.NEAREST).save(os.path.join(outdir, f"{tile}_labels.png"))
    rgb = np.zeros((W * H, 3), np.uint8)
    rgb[k[top]] = np.c_[r[top], g[top], b[top]].astype(np.uint16) >> 8 if r.max() > 255 else np.c_[r[top], g[top], b[top]]
    Image.fromarray(rgb.reshape(H, W, 3)).resize((W * 2, H * 2), Image.NEAREST).save(
        os.path.join(outdir, f"{tile}_rgb.png"))


if __name__ == "__main__":
    main()
