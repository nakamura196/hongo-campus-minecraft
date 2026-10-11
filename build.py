"""Turn a <tile>_cells.npz (classify.py output) into Bedrock /fill commands.

    python build.py CELLS.npz OUT.txt --rows R0 R1 --cols C0 C1 --at X Y Z [--preview P.png] [--java]

Buildings are cleaned up first (roofs.py: walls on the PLATEAU outline, roofs
as planes), then trees (trees.py: leaves off the walls, one trunk per tree);
--raw skips both. With --java, roofs and ground that fall between whole blocks get
slabs and stairs (smooth.py); --blocky keeps whole blocks only. Grass and the ground
under trees get warped slabs and stairs, drawn as grass by grasspack.py's resource pack
(--no-grass leaves them as whole blocks).

--java writes Java Edition block states, and if OUT ends in .zip, a data pack
(function hongo:load) to drop into saves/<world>/datapacks/.

One cell = one block. Row 0 is the north edge, so Minecraft z grows southward
with the row number and x eastward with the column. --at X Y Z is where the
north-west corner of the window lands, Y being the lowest ground in the window.

Each cell becomes a short stack of segments (block, y_from, y_to); identical
segments in neighbouring cells are merged into rectangles, so one /fill covers many cells.
The stack step is independent of the command step, so another writer
(.mcstructure, direct world editing) can reuse columns().
"""
import argparse, json, os, zipfile
import numpy as np
from PIL import Image
from scipy import ndimage as ndi
from roofs import regularise
from trees import separate
from smooth import surfaces, slab, stairs, relief, FAMILY

GROUND, BUILDING, TREE, OTHER, WATER, LOW = 0, 1, 2, 3, 4, 5
LEAVES = 'oak_leaves ["persistent_bit"=true]'   # without it the canopy decays
LOG = "oak_log"
FILL_MAX = 32768                                 # per-command block limit (Bedrock and Java)
JAVA = {LEAVES: "oak_leaves[persistent=true]",   # other block ids are the same in Java
        "weathered_copper": "waxed_weathered_copper", "oxidized_copper": "waxed_oxidized_copper"}
DATA_PACK_FORMAT = 121                           # Java 26.3


# Approximate colours of blocks, matched against the aerial RGB of the points.
WALLS = {
    "white_concrete": (207, 213, 214), "light_gray_concrete": (125, 125, 115),
    "gray_concrete": (55, 58, 62), "smooth_stone": (159, 159, 159),
    "andesite": (136, 136, 137), "polished_andesite": (132, 135, 134),
    "white_terracotta": (210, 178, 161), "light_gray_terracotta": (135, 107, 98),
    "terracotta": (152, 94, 68), "brown_terracotta": (77, 51, 36),
    "bricks": (150, 97, 83), "mud_bricks": (137, 103, 79),
    "smooth_sandstone": (223, 214, 170), "oxidized_copper": (82, 162, 132),
    "weathered_copper": (108, 153, 110), "cyan_terracotta": (87, 92, 92),
}
FLOOR = {
    "grass_block": (95, 145, 60), "stone": (125, 125, 125), "smooth_stone": (159, 159, 159),
    "gravel": (131, 127, 126), "dirt_path": (148, 122, 65), "coarse_dirt": (119, 85, 59),
}


SATURATE = 3.0      # aerial colours are washed out; push them away from grey before matching


def nearest(rgb, palette):
    """Name of the closest palette colour for each rgb row."""
    names = list(palette)
    pal = np.array([palette[n] for n in names], float)
    a = np.atleast_2d(rgb).astype(float)
    grey = a.mean(-1, keepdims=True)
    a = np.clip(grey + (a - grey) * SATURATE, 0, 255)
    d = ((a[:, None] - pal[None]) ** 2).sum(-1)
    return np.array(names, object)[d.argmin(1)]


def tinted(rgb):
    """Does a roof colour carry a real tint? Warm greys are everywhere (concrete,
    asphalt), so brown needs a clear tint; a green cast on a roof is copper."""
    rgb = np.asarray(rgb, float)
    sat = (rgb.max() - rgb.min()) / max(rgb.mean(), 1)
    return sat > 0.12 or (rgb.argmax() == 1 and sat > 0.04)


def wall_block(rgb):
    pal = {n: c for n, c in WALLS.items() if (max(c) - min(c)) / np.mean(c) > 0.15}
    if tinted(rgb) and np.argmax(rgb) == 1:                  # green cast: copper roof
        pal = {n: c for n, c in pal.items() if "copper" in n}
    elif tinted(rgb):
        pal = {n: c for n, c in pal.items() if "copper" not in n}
    if tinted(rgb):                     # match the hue first, lightness second
        hue = lambda c: (np.asarray(c, float) - np.mean(c)) / (np.ptp(c) + 1e-9)
        lum = lambda c: np.mean(c) / 255
        return min(pal, key=lambda n: 3 * ((hue(rgb) - hue(pal[n])) ** 2).sum()
                   + (lum(rgb) * 1.1 - lum(pal[n])) ** 2)
    greys = {n: c for n, c in WALLS.items() if n not in pal}
    return min(greys, key=lambda n: abs(np.mean(greys[n]) - np.mean(rgb) * 1.1))


def colours(lab, rgb, bid, cell=1.0):
    """Wall block per cell (one per PLATEAU building) and floor block per cell."""
    wall = np.full(lab.shape, "light_gray_concrete", object)
    b = (lab == BUILDING) & (bid >= 0)
    boxes = ndi.find_objects(np.where(b, bid + 1, 0))   # bounding box per building
    for i in np.unique(bid[b]):
        w = boxes[i]
        m = b[w] & (bid[w] == i)
        wall[w][m] = wall_block(np.median(rgb[w][m], 0))
    H, W = lab.shape                                   # floor: 5x5 m median colour, then 3x3 majority
    k = int(round(2.5 / cell))
    med = np.stack([ndi.median_filter(rgb[..., j].astype(float), 2 * k + 1, mode="nearest")
                    for j in range(3)], -1)
    floor = nearest(med.reshape(-1, 3), FLOOR).reshape(H, W)
    q = np.pad(floor, 1, mode="edge")
    nb = np.stack([q[dy:dy + H, dx:dx + W] for dy in range(3) for dx in range(3)])
    for name in FLOOR:
        floor[(nb == name).sum(0) >= 6] = name
    return wall, floor


def columns(lab, top, ground, base_y, rgb=None, bid=None, crown=None, trunk=None, cell=1.0, smooth=False,
            paint=None, cap=None, grass=False):
    """Per-cell stack: tuple of (block, y0, y1) inclusive, bottom to top. One block is `cell` m high.
    With rgb and bid, buildings and the ground take colours from the photo.
    With crown and trunk (trees.separate), leaves hang down to crown and trunks are logs.
    With smooth (Java ids, needs rgb and bid), roofs and ground get slabs and stairs (smooth.py).
    paint (colours()) and cap (smooth.surfaces()) can be passed in when the caller has them."""
    if rgb is not None:
        wall, floor = paint if paint is not None else colours(lab, rgb, bid, cell)
    ref = np.nanmin(ground)
    gu = (ground - ref) / cell + base_y
    tu = (top - ref) / cell + base_y
    gy = np.round(gu).astype(int)
    ty = np.round(np.where(np.isnan(tu), gu, tu)).astype(int)
    if cap is None:
        cap = surfaces(lab, tu, gu, bid, wall, floor, grass) if smooth else np.empty(lab.shape, object)
    n3, n4 = int(round(3 / cell)), int(round(4 / cell))
    stacks = np.empty(lab.shape, object)
    for (r, c), l in np.ndenumerate(lab):
        g, t = gy[r, c], max(ty[r, c], gy[r, c])
        s = [("dirt", base_y, g - 1)] if g > base_y else []
        if l == WATER:
            s.append(("water", g, g))
        elif rgb is not None:
            s.append((floor[r, c] if l in (GROUND, LOW) else "grass_block", g, g))
        elif l == LOW:
            s.append(("stone", g, g))                   # kerbs, paving edges
        else:
            s.append(("grass_block", g, g))
        if l == BUILDING and t > g:
            s.append((wall[r, c] if rgb is not None else "light_gray_concrete", g + 1, t))
        elif l == TREE and t > g and crown is not None:
            lo = max(g + 1, min(t, int(round((crown[r, c] - ref) / cell)) + base_y))
            if trunk[r, c] and lo > g + 1:
                s.append((LOG, g + 1, lo - 1))
            s.append((LEAVES, lo, t))
        elif l == TREE and t > g:
            s.append((LEAVES, max(g + 1, t - n4 + 1), t))   # canopy only, 4 m thick
        elif l == OTHER and t > g:
            s.append(("cobblestone", g + 1, min(t, g + n3)))
        if cap[r, c] is not None:          # rebuild the top: whole blocks up to k, then the cap
            k, (kind, block, *facing) = cap[r, c]
            if l == BUILDING:
                s = [seg for seg in s if seg[1] <= g] + [(wall[r, c], g + 1, k)]
            else:                           # the ground block may move down or up by one
                above = [seg for seg in s if seg[1] > g]       # trunk, leaves over the ground
                s = [("dirt", base_y, k - 1), (next(seg for seg in s if seg[1] == g)[0], k, k)]
                lo = k + 2 if kind == "slab" else k + 1
                s += [(b, max(y0, lo), y1) for b, y0, y1 in above]
            if kind == "slab":
                s.append((slab(block), k + 1, k + 1))
            else:
                s[-1] = (stairs(block, facing[0]), k, k)
        stacks[r, c] = tuple(seg for seg in s if seg[1] <= seg[2])
    return stacks


def rectangles(mask):
    """Greedy cover of a boolean grid by (r0, c0, r1, c1) rectangles."""
    m = mask.copy()
    H, W = m.shape
    for r, c in zip(*np.nonzero(mask)):
        if not m[r, c]:
            continue
        c1 = c
        while c1 + 1 < W and m[r, c1 + 1]:
            c1 += 1
        r1 = r
        while r1 + 1 < H and m[r1 + 1, c:c1 + 1].all():
            r1 += 1
        m[r:r1 + 1, c:c1 + 1] = False
        yield r, c, r1, c1


def commands(stacks, x0, z0, names=None):
    """Dirt goes in one-block layers (wide rectangles); every other segment is
    merged with the cells that carry exactly the same segment."""
    H, W = stacks.shape
    cells = {}
    for (r, c), s in np.ndenumerate(stacks):
        for block, y0, y1 in s:
            keys = [(block, y, y) for y in range(y0, y1 + 1)] if block == "dirt" else [(block, y0, y1)]
            for k in keys:
                cells.setdefault(k, []).append((r, c))
    out = []
    for (block, y0, y1), rc in sorted(cells.items(), key=lambda kv: kv[0][1]):
        mask = np.zeros((H, W), bool)
        mask[tuple(np.array(rc).T)] = True
        for r0, c0, r1, c1 in rectangles(mask):
            per_row = (c1 - c0 + 1) * (y1 - y0 + 1)     # split rows if over the limit
            step = max(1, FILL_MAX // per_row)
            for a in range(r0, r1 + 1, step):
                b = min(r1, a + step - 1)
                name = (names or {}).get(block, block)
                out.append(f"fill {x0 + c0} {y0} {z0 + a} {x0 + c1} {y1} {z0 + b} {name}")
    return out


def preview(stacks, path):
    """Top view coloured by the top block, shaded by height."""
    col = {**WALLS, **FLOOR, "water": (40, 70, 200), LEAVES: (30, 110, 40),
           "cobblestone": (110, 110, 110), "dirt": (130, 90, 60), LOG: (100, 80, 50)}
    for base, pair in FAMILY.items():               # slabs and stairs: colour of the block they stand for
        for name in pair:
            col.setdefault(name, col[base])
    H, W = stacks.shape
    rgb = np.zeros((H, W, 3))
    h = np.array([[s[-1][2] for s in row] for row in stacks], float)
    shade = 0.6 + 0.4 * (h - h.min()) / max(1, np.ptp(h))
    for (r, c), s in np.ndenumerate(stacks):
        n = s[-1][0]; rgb[r, c] = np.array(col[n if n in col else n.split("[")[0]]) * shade[r, c]
    Image.fromarray(rgb.astype(np.uint8)).resize((W * 4, H * 4), Image.NEAREST).save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cells"); ap.add_argument("out")
    ap.add_argument("--rows", type=int, nargs=2, required=True)
    ap.add_argument("--cols", type=int, nargs=2, required=True)
    ap.add_argument("--at", type=int, nargs=3, required=True, metavar=("X", "Y", "Z"))
    ap.add_argument("--preview")
    ap.add_argument("--java", action="store_true")
    ap.add_argument("--plain", action="store_true", help="ignore the photo colours")
    ap.add_argument("--raw", action="store_true", help="keep raw roofs and outlines (no roofs.regularise)")
    ap.add_argument("--blocky", action="store_true", help="whole blocks only (no slabs or stairs)")
    ap.add_argument("--no-grass", dest="grass", action="store_false",
                    help="no slabs or stairs on grass and under trees (they need grasspack.py's resource pack)")
    ap.add_argument("--name", default="load", help="function name in the data pack (hongo:<name>)")
    ap.add_argument("--piece", type=int, help="split into functions <name>_<i> of PIECE x PIECE blocks "
                    "(a multiple of 16), so each fits one forceload (256 chunks)")
    a = ap.parse_args()
    d = np.load(a.cells)
    win = np.s_[a.rows[0]:a.rows[1], a.cols[0]:a.cols[1]]
    photo = "rgb" in d and not a.plain
    cell = float(d["cell"]) if "cell" in d else 1.0
    lab, top, ground = d["label"][win], d["top"][win], d["ground"][win]
    crown = trunk = None
    if "bid" in d and not a.raw:
        lab, top, report = regularise(lab, top, ground, d["bid"][win], d["half"][win] if "half" in d else None, cell)
        print(", ".join(f"{k} {v}" for k, v in report.items()))
        lab, crown, trunk, report = separate(lab, top, ground, cell)
        print(", ".join(f"{k} {v}" for k, v in report.items()))
    smooth = a.java and photo and not a.blocky
    paint = colours(lab, d["rgb"][win], d["bid"][win], cell) if photo else None
    cap = None
    if "bid" in d:                        # same heights as columns(), so the caps can be shared
        ref = np.nanmin(ground)
        tu, gu = (top - ref) / cell + a.at[1], (ground - ref) / cell + a.at[1]
        bid = d["bid"][win]
        cap = surfaces(lab, tu, gu, bid, *paint, grass=a.grass) if smooth else None
        before = relief(lab, tu, gu, bid, trees=a.grass)
        after = relief(lab, tu, gu, bid, cap, trees=a.grass) if smooth else None
        print("relief (mean |built - measured| in blocks, jumps > 0.6 block):", before, "->", after)
    stacks = columns(lab, top, ground, a.at[1],
                     d["rgb"][win] if photo else None, d["bid"][win] if photo else None, crown, trunk, cell, smooth,
                     paint, cap)
    names = JAVA if a.java else None
    if a.piece:
        assert a.piece % 16 == 0 and a.at[0] % 16 == 0 and a.at[2] % 16 == 0, "pieces must be chunk aligned"
        parts = {}
        for r in range(0, stacks.shape[0], a.piece):
            for c in range(0, stacks.shape[1], a.piece):
                sub = stacks[r:r + a.piece, c:c + a.piece]
                x, z = a.at[0] + c, a.at[2] + r
                key = f"{a.name}_{len(parts)}"
                parts[key] = ((x, z, x + sub.shape[1] - 1, z + sub.shape[0] - 1),
                              commands(sub, x, z, names))
        cmds = [c for _, cs in parts.values() for c in cs]
    else:
        parts = {a.name: (None, commands(stacks, a.at[0], a.at[2], names))}
        cmds = parts[a.name][1]
    ys = [y for s in stacks.flat for seg in s for y in seg[1:]]
    H, W = stacks.shape
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    head = (f"# {a.cells} rows {a.rows} cols {a.cols} -> x {a.at[0]}..{a.at[0] + W - 1}"
            f" y {min(ys)}..{max(ys)} z {a.at[2]}..{a.at[2] + H - 1}\n")
    if a.out.endswith(".zip"):
        meta = {"pack": {"description": "Hongo campus from Tokyo LiDAR + PLATEAU",
                         "pack_format": DATA_PACK_FORMAT,
                         "min_format": DATA_PACK_FORMAT, "max_format": DATA_PACK_FORMAT}}
        with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("pack.mcmeta", json.dumps(meta, indent=2))
            for key, (box, cs) in parts.items():
                z.writestr(f"data/hongo/function/{key}.mcfunction", head + "\n".join(cs) + "\n")
                if box:
                    print(f"{key}: x {box[0]}..{box[2]} z {box[1]}..{box[3]}, {len(cs)} commands")
    else:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(head + "\n".join(cmds) + "\n")
    print(f"{H}x{W} cells -> {len(cmds)} commands, y {min(ys)}..{max(ys)}")
    if a.preview:
        preview(stacks, a.preview)


if __name__ == "__main__":
    main()
