"""Half blocks on top: slabs and stairs where a roof or the ground falls between whole blocks.

    from smooth import surfaces
    cap = surfaces(lab, tu, gu, bid, wall, floor)

At 1 m a gentle slope comes out as a staircase of 1 m steps, each a jump for the
player, and every height is rounded by up to 0.5 m. Here the surface is rounded to
0.5 m instead:

  - a cell whose surface falls on a half block gets a bottom slab on top;
  - a cell on a steady slope (rising at least RISE blocks per cell on both sides
    along one axis, no cliff to either neighbour) gets stairs instead of its top
    block, the high side uphill, so a 1 m step becomes two 0.5 m steps.

Only exposed ground (GROUND, LOW) and roofs (BUILDING) are touched, and only where
the block has a slab or stairs of a similar colour (grass, dirt path and water have none).
With grass=True, grass and the ground under trees are touched too, using warped slabs
and stairs that grasspack.py redraws as grass (the game has no grass slab).
Roof neighbours count only within the same PLATEAU building, so stairs never form
against a wall. Block names are Java Edition ids.
"""
import numpy as np

GROUND, BUILDING, TREE, OTHER, WATER, LOW = 0, 1, 2, 3, 4, 5
RISE = 0.25        # blocks per cell, on both sides, for stairs
# block -> (slab, stairs) of the same or a close colour
FAMILY = {
    "smooth_stone": ("smooth_stone_slab", "stone_stairs"), "stone": ("stone_slab", "stone_stairs"),
    "andesite": ("andesite_slab", "andesite_stairs"),
    "polished_andesite": ("polished_andesite_slab", "polished_andesite_stairs"),
    "bricks": ("brick_slab", "brick_stairs"), "mud_bricks": ("mud_brick_slab", "mud_brick_stairs"),
    "smooth_sandstone": ("smooth_sandstone_slab", "smooth_sandstone_stairs"),
    "weathered_copper": ("waxed_weathered_cut_copper_slab", "waxed_weathered_cut_copper_stairs"),
    "oxidized_copper": ("waxed_oxidized_cut_copper_slab", "waxed_oxidized_cut_copper_stairs"),
    "white_concrete": ("smooth_quartz_slab", "smooth_quartz_stairs"),
    "light_gray_concrete": ("stone_slab", "stone_stairs"),
    "gray_concrete": ("polished_deepslate_slab", "polished_deepslate_stairs"),
    "white_terracotta": ("cherry_slab", "cherry_stairs"),
    "light_gray_terracotta": ("polished_granite_slab", "polished_granite_stairs"),
    "terracotta": ("polished_granite_slab", "polished_granite_stairs"),
    "brown_terracotta": ("dark_oak_slab", "dark_oak_stairs"),
    "cyan_terracotta": ("cobbled_deepslate_slab", "cobbled_deepslate_stairs"),
    "gravel": ("andesite_slab", "andesite_stairs"),
    "coarse_dirt": ("mud_brick_slab", "mud_brick_stairs"),
}
GRASS = ("warped_slab", "warped_stairs")      # redrawn as grass by grasspack.py
# (dr, dc, facing of the high side)
DIRS = ((-1, 0, "north"), (1, 0, "south"), (0, -1, "west"), (0, 1, "east"))


def family(block):
    return GRASS if block == "grass_block" else FAMILY[block]


def slab(block):
    return f"{family(block)[0]}[type=bottom]"


def stairs(block, facing):
    return f"{family(block)[1]}[facing={facing},half=bottom,shape=straight]"


def surfaces(lab, tu, gu, bid, wall, floor, grass=False):
    """Per cell: (top block y, cap) for the cells it changes, None elsewhere.
    tu, gu: continuous heights in blocks of the top block and the ground block
    (rounding them gives what columns() builds without smoothing).
    cap is ("slab", block) on top of y, or ("stairs", block, facing) replacing y.
    grass: also grass and the ground under trees (whose block is grass_block)."""
    H, W = lab.shape
    roof = (lab == BUILDING) & ~np.isnan(tu)
    ground = np.isin(lab, (GROUND, LOW, TREE) if grass else (GROUND, LOW))
    u = np.where(roof, tu, gu)
    blk = np.where(roof, wall, np.where(lab == TREE, "grass_block", floor))
    has = (lambda b: b in FAMILY or b == "grass_block") if grass else (lambda b: b in FAMILY)
    ok = (roof | ground) & np.vectorize(has, otypes=[bool])(blk)
    q = np.round(2 * u) / 2                       # surface to the half block
    out = np.empty((H, W), object)
    for r, c in zip(*np.nonzero(ok)):
        k = int(np.floor(q[r, c]))
        if q[r, c] != k:
            out[r, c] = (k, ("slab", blk[r, c]))
            continue
        best = None
        for dr, dc, facing in DIRS:
            a, b = (r + dr, c + dc), (r - dr, c - dc)           # uphill, downhill
            if not (0 <= a[0] < H and 0 <= a[1] < W and 0 <= b[0] < H and 0 <= b[1] < W):
                continue
            same = (lambda p: roof[p] and bid[p] == bid[r, c]) if roof[r, c] else (lambda p: ground[p])
            if not (same(a) and same(b)):
                continue
            up, down = u[a] - u[r, c], u[r, c] - u[b]
            if up >= RISE and down >= RISE and q[a] <= k + 1 and q[b] >= k - 1:
                if best is None or up + down > best[0]:
                    best = (up + down, facing)
        if best:
            out[r, c] = (k, ("stairs", blk[r, c], best[1]))
    return out


def relief(lab, tu, gu, bid, cap=None, trees=False):
    """How the built surface compares with the measured one, on a 2x2 grid per cell
    (stairs have a low and a high half). Returns mean |built - measured| in blocks and
    the number of neighbouring half-cells, both exposed ground or both one roof, that differ
    by more than 0.6 block (a jump for the player) where the measured heights differ by less
    than 1.5 (not a real wall). trees: count the ground under trees as ground."""
    roof = (lab == BUILDING) & ~np.isnan(tu)
    ground = np.isin(lab, (GROUND, LOW, TREE) if trees else (GROUND, LOW))
    u = np.where(roof, tu, gu)
    s = np.round(u) + 1.0                          # surface of the top block
    H, W = lab.shape
    fine = np.repeat(np.repeat(s, 2, 0), 2, 1)
    if cap is not None:
        for (r, c), v in np.ndenumerate(cap):
            if v is None:
                continue
            k, kind = v
            if kind[0] == "slab":
                fine[2 * r:2 * r + 2, 2 * c:2 * c + 2] = k + 1.5
            else:
                fine[2 * r:2 * r + 2, 2 * c:2 * c + 2] = k + 0.5
                f = kind[2]
                if f == "north": fine[2 * r, 2 * c:2 * c + 2] = k + 1
                if f == "south": fine[2 * r + 1, 2 * c:2 * c + 2] = k + 1
                if f == "west": fine[2 * r:2 * r + 2, 2 * c] = k + 1
                if f == "east": fine[2 * r:2 * r + 2, 2 * c + 1] = k + 1
    meas = np.repeat(np.repeat(u + 1.0, 2, 0), 2, 1)
    group = np.where(roof, bid, np.where(ground, -2, -3))
    group = np.repeat(np.repeat(group, 2, 0), 2, 1)
    res = {}
    for name, m in (("ground", ground), ("roof", roof)):
        mf = np.repeat(np.repeat(m, 2, 0), 2, 1)
        err = float(np.abs(fine - meas)[mf].mean())
        jumps = 0
        for ax in (0, 1):
            a = np.s_[1:, :] if ax == 0 else np.s_[:, 1:]
            z = np.s_[:-1, :] if ax == 0 else np.s_[:, :-1]
            same = mf[a] & mf[z] & (group[a] == group[z]) & (group[a] != -3)
            jumps += int((same & (np.abs(fine[a] - fine[z]) > 0.6) & (np.abs(meas[a] - meas[z]) < 1.5)).sum())
        res[name] = (round(err, 3), jumps)
    return res
