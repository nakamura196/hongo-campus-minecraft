"""Split the canopy into single trees, drop leaves stuck to walls, give each tree a trunk.

    from trees import separate
    lab, crown, trunk, report = separate(lab, top, ground, cell=1.0)

Run after roofs.regularise, so building cells already sit on the PLATEAU outline.
The raw TREE cells hug the walls: pulses that graze a roof edge return twice
and look like leaves, and real crowns that reach a wall become a sheet of
leaves glued to it. Here:

  1. Leaves within 1 m of a building (8-neighbours at 1 m cells) become ground,
     leaving a gap of 1 m between wall and crown.
  2. The rest is cut into single trees by watershed on the smoothed canopy
     height, seeded at its local maxima at least MIN_DIST m apart.
     Trees smaller than MIN_TREE cells (edge returns, shrub fragments) become ground.
  3. Each tree gets a crown depth of half its height (CROWN_MIN..CROWN_MAX m)
     and a flat underside at that depth; its highest cell gets the trunk.

crown: height (m) of the underside of the leaves per cell, NaN off trees.
trunk: bool, the cell of each tree that carries a log from the ground to the crown.
with_seg=True also returns seg, the tree number of each cell (0 off trees).
"""
import numpy as np
from scipy import ndimage as ndi
from skimage.feature import peak_local_max
from skimage.segmentation import watershed

GROUND, BUILDING, TREE = 0, 1, 2
MIN_DIST = 3          # m between tree tops (crown radius of a small campus tree)
MIN_TREE = 4          # m2 (cells at 1 m); smaller groups are not trees
CROWN_MIN, CROWN_MAX = 3.0, 8.0


def separate(lab, top, ground, cell=1.0, with_seg=False):
    lab = lab.copy()
    t0 = lab == TREE
    g = 2 * int(round(1 / cell)) + 1
    wall = ndi.binary_dilation(lab == BUILDING, np.ones((g, g), bool)) & t0
    lab[wall] = GROUND
    T = lab == TREE
    h = np.where(T, np.nan_to_num(top - ground), 0)
    sm = ndi.gaussian_filter(h, 1.0 / cell)
    peaks = peak_local_max(sm, min_distance=int(round(MIN_DIST / cell)), labels=ndi.label(T)[0],
                           exclude_border=False)           # every patch gets at least one top
    markers = np.zeros(lab.shape, int)
    markers[tuple(peaks.T)] = np.arange(1, len(peaks) + 1)
    seg = watershed(-sm, markers, mask=T)
    size = np.bincount(seg.ravel(), minlength=len(peaks) + 1)
    small = T & ((seg == 0) | (size[seg] < MIN_TREE / cell ** 2))
    lab[small] = GROUND
    T &= ~small
    seg[~T] = 0

    crown = np.full(lab.shape, np.nan)
    trunk = np.zeros(lab.shape, bool)
    boxes = ndi.find_objects(seg)                          # bounding box per tree
    for i in np.nonzero(size >= MIN_TREE / cell ** 2)[0]:
        if i == 0:
            continue
        w = boxes[i - 1]
        m = seg[w] == i
        tt = top[w][m]
        hi = np.nanmax(tt)
        depth = np.clip(0.5 * np.nanmax(h[w][m]), CROWN_MIN, CROWN_MAX)
        crown[w][m] = np.minimum(hi - depth, tt - 2)       # flat underside, thin at the rim
        r, c = np.argwhere(m)[np.nanargmax(np.where(m, top[w], -np.inf)[m])]
        trunk[r + w[0].start, c + w[1].start] = True
    report = {"leaves on walls removed": int(wall.sum()),
              "trees": int(trunk.sum()),
              "fragments removed": int(small.sum())}
    if with_seg:                            # mesh.py: which tree each cell belongs to (0 = none)
        return lab, crown, trunk, report, seg
    return lab, crown, trunk, report
