"""Clean up buildings before they become blocks: walls on the PLATEAU outline, roofs as planes.

    from roofs import regularise
    lab, top, report = regularise(lab, top, ground, bid, half, cell=1.0)

The raw cells take the label and height of the highest LiDAR point, so walls
fray where trees or eaves cross the outline, and roofs carry every antenna,
railing and stray return as a one-block bump. Here:

  0. Edges inside a roof: where the highest point of a cell stands more than
     EDGE m above the height that covers half of it (classify.py, `half`), a
     higher part (a tower) only grazes the cell; it takes the half height.
     Otherwise every tower on a hall comes out one cell too wide on each side.
  1. Outline: a cell inside a PLATEAU outline that stands >= MIN_HEIGHT above the
     ground is building, whatever its top point was (overhanging trees, dark
     glass with no return); lower ones are courtyards and become ground, as do
     building cells outside every outline.
  2. Roof facets: heights are 3x3-median filtered within the building, then
     cut into facets by flood fill (neighbours closer than STEP m stay together).
  3. Each facet gets a plane by RANSAC; a nearly flat plane is made level.
     Cells more than KEEP m off the plane (a tower on a hall, a curved roof)
     are cut into facets again and fitted again. What is left in groups
     smaller than MIN_FACET m2 (antennas, units, railings) takes the height
     of the nearest fitted roof cell.
"""
import warnings
from collections import deque
import numpy as np
from scipy import ndimage as ndi

GROUND, BUILDING, TREE, OTHER, WATER, LOW = 0, 1, 2, 3, 4, 5
MIN_HEIGHT = 2.0      # m above ground to count as part of a building inside its outline
STEP = 0.75           # m; a larger jump between neighbours starts a new roof facet
MIN_FACET = 6         # m2 (cells at 1 m); smaller facets are noise (antennas, units, railings)
INLIER = 0.25         # m; RANSAC tolerance
KEEP = 0.6            # m; cells further than this from their plane are fitted again
EDGE = 1.5            # m; see step 0
FLAT = 0.08           # slope (rise per m) below which a facet is made level
N4 = ((1, 0), (-1, 0), (0, 1), (0, -1))


def _median3(h, m):
    """3x3 median of h over the cells of mask m only (NaN elsewhere)."""
    H, W = h.shape
    p = np.pad(np.where(m, h, np.nan), 1, constant_values=np.nan)
    st = np.stack([p[dy:dy + H, dx:dx + W] for dy in range(3) for dx in range(3)])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)     # all-NaN windows
        return np.where(m, np.nanmedian(st, 0), np.nan)


def _fill(h, m):
    """Give NaN cells of mask m the height of the nearest known cell of m."""
    from scipy.ndimage import distance_transform_edt
    known = m & ~np.isnan(h)
    if not known.any():
        return h
    _, (iy, ix) = distance_transform_edt(~known, return_indices=True)
    return np.where(m & np.isnan(h), h[iy, ix], h)


def _facets(h, m):
    """Label array: flood-fill facets of mask m, 4-neighbours within STEP m."""
    seg = np.full(h.shape, -1, int)
    n = 0
    for r, c in zip(*np.nonzero(m)):
        if seg[r, c] >= 0:
            continue
        seg[r, c] = n
        q = deque([(r, c)])
        while q:
            y, x = q.popleft()
            for dy, dx in N4:
                v, u = y + dy, x + dx
                if 0 <= v < h.shape[0] and 0 <= u < h.shape[1] and m[v, u] and seg[v, u] < 0 \
                        and abs(h[v, u] - h[y, x]) <= STEP:
                    seg[v, u] = n
                    q.append((v, u))
        n += 1
    return seg, n


def _at(A, coef):
    # written out: numpy's matmul on macOS Accelerate raises spurious FP warnings here
    return A[:, 0] * coef[0] + A[:, 1] * coef[1] + coef[2]


def _plane(ys, xs, zs, rng, cell=1.0):
    """RANSAC plane through the cells (x, y, z): its height at each cell, levelled if nearly flat."""
    A = np.c_[xs * cell, ys * cell, np.ones(len(xs))]
    best = np.ones(len(zs), bool)
    best_n = -1
    for _ in range(60 if len(zs) > 3 else 0):
        i = rng.choice(len(zs), 3, replace=False)
        if abs(np.linalg.det(A[i])) < 0.5 * cell * cell:                  # collinear sample (cells are on a grid)
            continue
        coef = np.linalg.solve(A[i], zs[i])
        inl = np.abs(_at(A, coef) - zs) < INLIER
        if inl.sum() > best_n:
            best, best_n = inl, inl.sum()
    if best.sum() < 3:
        best[:] = True
    coef = np.linalg.lstsq(A[best], zs[best], rcond=None)[0]
    if np.hypot(coef[0], coef[1]) < FLAT:
        return np.full(len(zs), np.median(zs[best]))
    return _at(A, coef)


def _fit(h, m, rng, cell=1.0, depth=0):
    """Roof heights for mask m: planes on facets, peeling off what a plane does not fit
    (a tower on a hall, a dome) and fitting that again. NaN where nothing fits."""
    fit = np.full(h.shape, np.nan)
    seg, n = _facets(h, m)
    size = np.bincount(seg[m], minlength=n)
    min_facet = max(3, int(round(MIN_FACET / cell ** 2)))
    for f in np.nonzero(size >= min_facet)[0]:
        ys, xs = np.nonzero(seg == f)
        z = _plane(ys.astype(float), xs.astype(float), h[ys, xs], rng, cell)
        ok = np.abs(z - h[ys, xs]) <= KEEP
        fit[ys[ok], xs[ok]] = z[ok]
        rest = np.zeros(h.shape, bool)
        rest[ys[~ok], xs[~ok]] = True
        if depth < 4 and rest.sum() >= min_facet:
            sub = _fit(h, rest, rng, cell, depth + 1)
            fit[rest] = sub[rest]
    return fit


def regularise(lab, top, ground, bid, half=None, cell=1.0):
    lab, top = lab.copy(), top.astype(float).copy()
    raw = top.copy()
    edge = np.zeros(top.shape, bool)
    if half is not None:
        edge = (bid >= 0) & (top - half > EDGE)
        top[edge] = half[edge]
    was = lab == BUILDING
    hag = np.where(np.isnan(top), 0, top - ground)
    inside = bid >= 0
    lab[was & ~inside] = GROUND
    top[was & ~inside] = np.nan
    low = inside & (lab == BUILDING) & (hag < MIN_HEIGHT)   # courtyards, light wells
    lab[low] = GROUND
    bld = inside & ((hag >= MIN_HEIGHT) | (lab == WATER))
    lab[bld] = BUILDING
    rng = np.random.default_rng(0)
    out = np.full(top.shape, np.nan)
    nleft = 0
    boxes = ndi.find_objects(np.where(bld, bid + 1, 0))      # bounding box per building
    for b in np.unique(bid[bld]):
        win = boxes[b]
        mw = bld[win] & (bid[win] == b)
        h = np.where(mw & (hag[win] >= MIN_HEIGHT), top[win], np.nan)   # trees, glass: unknown
        h = _fill(_median3(h, mw & ~np.isnan(h)), mw)
        if np.isnan(h[mw]).any():          # no usable roof at all: drop the building
            lab[win][mw] = GROUND
            continue
        fit = _fit(h, mw, rng, cell)
        small = mw & np.isnan(fit)
        nleft += int(small.sum())
        if small.all():                     # tiny building: one level
            fit[mw] = np.median(h[mw])
        else:
            fit = _fill(np.where(small, np.nan, fit), mw)   # leftovers take a neighbour's roof
        out[win][mw] = fit[mw]
    top[bld] = out[bld]
    keep = bld & ~np.isnan(top)
    report = {
        "buildings": int(len(np.unique(bid[bld]))), "cells levelled to a neighbour": nleft,
        "cells now building": int(bld.sum()), "were building": int(was.sum()),
        "building cells outside outline removed": int((was & ~inside).sum()),
        "low building cells made ground": int(low.sum()),
        "added inside outline (trees, no return)": int((bld & ~was).sum()),
        "edge cells lowered to the half height": int(edge.sum()),
    }
    d = (top - raw)[keep & was & ~np.isnan(raw)]
    report["roof change vs raw top (m): median |d|, p95 |d|"] = (round(float(np.median(np.abs(d))), 2),
                                                                 round(float(np.percentile(np.abs(d), 95)), 2))
    return lab, top.astype(np.float32), report


def steps(lab, top, ground, bid, cell=1.0):
    """Count of neighbouring building cells (same building) whose block heights differ."""
    ty = np.round((np.where(np.isnan(top), ground, top) - np.nanmin(ground)) / cell)
    b = (lab == BUILDING) & (bid >= 0)
    n = 0
    for ax in (0, 1):
        a = np.s_[1:, :] if ax == 0 else np.s_[:, 1:]
        z = np.s_[:-1, :] if ax == 0 else np.s_[:, :-1]
        same = b[a] & b[z] & (bid[a] == bid[z])
        n += int((same & (ty[a] != ty[z])).sum())
    return n
