"""Turn a <tile>_cells.npz (classify.py output) into Bedrock /fill commands.

    python build.py CELLS.npz OUT.txt --rows R0 R1 --cols C0 C1 --at X Y Z [--preview P.png]

One cell = one block. Row 0 is the north edge, so Minecraft z grows southward
with the row number and x eastward with the column. --at X Y Z is where the
north-west corner of the window lands, Y being the lowest ground in the window.

Each cell becomes a short stack of segments (block, y_from, y_to); identical
segments in neighbouring cells are merged into rectangles, so one /fill covers many cells.
The stack step is independent of the command step, so another writer
(.mcstructure, direct world editing) can reuse columns().
"""
import argparse
import numpy as np
from PIL import Image

GROUND, BUILDING, TREE, OTHER, WATER, LOW = 0, 1, 2, 3, 4, 5
LEAVES = 'oak_leaves ["persistent_bit"=true]'   # without it the canopy decays
FILL_MAX = 32768                                 # Bedrock's per-command block limit


def columns(lab, top, ground, base_y):
    """Per-cell stack: tuple of (block, y0, y1) inclusive, bottom to top."""
    ref = np.nanmin(ground)
    gy = (np.round(ground - ref) + base_y).astype(int)
    ty = np.round(np.where(np.isnan(top), ground, top) - ref).astype(int) + base_y
    stacks = np.empty(lab.shape, object)
    for (r, c), l in np.ndenumerate(lab):
        g, t = gy[r, c], max(ty[r, c], gy[r, c])
        s = [("dirt", base_y, g - 1)] if g > base_y else []
        if l == WATER:
            s.append(("water", g, g))
        elif l == LOW:
            s.append(("stone", g, g))                   # kerbs, paving edges
        else:
            s.append(("grass_block", g, g))
        if l == BUILDING and t > g:
            s.append(("light_gray_concrete", g + 1, t))
        elif l == TREE and t > g:
            s.append((LEAVES, max(g + 1, t - 3), t))   # canopy only, 4 thick
        elif l == OTHER and t > g:
            s.append(("cobblestone", g + 1, min(t, g + 3)))
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


def commands(stacks, x0, z0):
    """Dirt goes in one-block layers (wide rectangles); every other segment is
    merged with the cells that carry exactly the same segment."""
    H, W = stacks.shape
    groups = {}
    for (r, c), s in np.ndenumerate(stacks):
        for block, y0, y1 in s:
            keys = [(block, y, y) for y in range(y0, y1 + 1)] if block == "dirt" else [(block, y0, y1)]
            for k in keys:
                groups.setdefault(k, np.zeros((H, W), bool))[r, c] = True
    out = []
    for (block, y0, y1), mask in sorted(groups.items(), key=lambda kv: kv[0][1]):
        for r0, c0, r1, c1 in rectangles(mask):
            per_row = (c1 - c0 + 1) * (y1 - y0 + 1)     # split rows if over the limit
            step = max(1, FILL_MAX // per_row)
            for a in range(r0, r1 + 1, step):
                b = min(r1, a + step - 1)
                out.append(f"fill {x0 + c0} {y0} {z0 + a} {x0 + c1} {y1} {z0 + b} {block}")
    return out


def preview(stacks, path):
    """Top view coloured by the top block, shaded by height."""
    col = {"grass_block": (90, 160, 60), "water": (40, 70, 200), "stone": (150, 150, 150),
           "light_gray_concrete": (200, 200, 200), LEAVES: (30, 110, 40),
           "cobblestone": (110, 110, 110), "dirt": (130, 90, 60)}
    H, W = stacks.shape
    rgb = np.zeros((H, W, 3))
    h = np.array([[s[-1][2] for s in row] for row in stacks], float)
    shade = 0.6 + 0.4 * (h - h.min()) / max(1, np.ptp(h))
    for (r, c), s in np.ndenumerate(stacks):
        rgb[r, c] = np.array(col[s[-1][0]]) * shade[r, c]
    Image.fromarray(rgb.astype(np.uint8)).resize((W * 4, H * 4), Image.NEAREST).save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cells"); ap.add_argument("out")
    ap.add_argument("--rows", type=int, nargs=2, required=True)
    ap.add_argument("--cols", type=int, nargs=2, required=True)
    ap.add_argument("--at", type=int, nargs=3, required=True, metavar=("X", "Y", "Z"))
    ap.add_argument("--preview")
    a = ap.parse_args()
    d = np.load(a.cells)
    win = np.s_[a.rows[0]:a.rows[1], a.cols[0]:a.cols[1]]
    stacks = columns(d["label"][win], d["top"][win], d["ground"][win], a.at[1])
    cmds = commands(stacks, a.at[0], a.at[2])
    ys = [y for s in stacks.flat for seg in s for y in seg[1:]]
    H, W = stacks.shape
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(f"# {a.cells} rows {a.rows} cols {a.cols} -> x {a.at[0]}..{a.at[0] + W - 1}"
                f" y {min(ys)}..{max(ys)} z {a.at[2]}..{a.at[2] + H - 1}\n")
        f.write("\n".join(cmds) + "\n")
    print(f"{H}x{W} cells -> {len(cmds)} commands, y {min(ys)}..{max(ys)}")
    if a.preview:
        preview(stacks, a.preview)


if __name__ == "__main__":
    main()
