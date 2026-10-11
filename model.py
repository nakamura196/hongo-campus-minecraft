"""A 3D model of one building, coloured from the photos: the display-independent layer in EPSG:6677.

    .venv/bin/python model.py out/yasuda_photo/refined/model out/yasuda_photo/model --tiles 09LD0853 09LD0854 09LD0863 09LD0864 --center -6454 -31806

Shape (LiDAR only, nothing added by hand):
  - a height model (DSM) on a 0.25 m grid turned to the building's own axes (from the PLATEAU
    footprint's minimum rotated rectangle), so the walls come out straight, not as staircases
  - every cell is a flat top; wherever a neighbour is lower (or outside the footprint, down to the
    ground) a vertical wall closes the step. Walls are cut into 0.125 m rows to carry colour
Colour (from the photos, aligned by align.py + refine.py):
  - every vertex is projected into every photo that faces it (angle under 75 degrees) and sees it:
    a depth buffer per photo is made from the model itself and the LiDAR points around it (trees,
    other buildings), so a tree in front does not paint the wall
  - per photo and channel an exposure gain is fitted to the consensus, then the colour is the
    weighted median (weight: facing / distance)
  - where no photo sees a vertex (roofs, the back), the colour comes from the LiDAR points (their
    colour is from the aerial photo). Those vertices are a separate part, named lidar_fallback, so
    what came from the photos stays apart from what did not
  - a wall needs --min-views photos to take a photo colour; walls short of that (behind the hedges
    in every photo, the back) are one flat grey, in their own part unseen_wall
Writes:
  <out>/yasuda.glb          glTF, local axes like mesh.py (x east, y up, z south; origin in extras)
  <out>/yasuda_lidar.glb    the LiDAR points of the same area, same axes (the "before")
  <out>/yasuda.ply          the coloured vertices in absolute EPSG:6677 with a source flag
  <out>/yasuda.json         origin, sources (attribution), coverage
"""
import argparse, json, os, struct
import numpy as np, pycolmap
from PIL import Image
from scipy import ndimage
from shapely.geometry import shape, Point
from shapely import contains_xy
import align
from mesh import Part, Cloud, write_glb

np.seterr(all="ignore")
CELL, ROW = 0.25, 0.125
UNSEEN = np.array([150, 150, 150], np.uint8)   # walls no photo sees: flat grey, not a guess


def frame(target):
    """Origin and axes of the building: the longer side of the minimum rotated rectangle is u."""
    r = np.array(target.minimum_rotated_rectangle.exterior.coords)[:4]
    e = r[1] - r[0] if np.linalg.norm(r[1] - r[0]) >= np.linalg.norm(r[2] - r[1]) else r[2] - r[1]
    th = np.arctan2(e[1], e[0])
    return np.array(target.centroid.coords[0]), np.array([np.cos(th), np.sin(th)]), np.array([-np.sin(th), np.cos(th)])


def dsm(P, K, C, target, o, ax, bx, ground_at):
    """Height, colour and inside-mask on the building grid."""
    loc = np.column_stack([(P[:, :2] - o) @ ax, (P[:, :2] - o) @ bx])
    g = np.array(target.buffer(0.5).exterior.coords)
    gl = np.column_stack([(g - o) @ ax, (g - o) @ bx])
    u0, v0 = gl.min(0) - 2 * CELL; nu, nv = (np.ceil((gl.max(0) - gl.min(0)) / CELL) + 4).astype(int)
    iu = ((loc[:, 0] - u0) / CELL).astype(int); iv = ((loc[:, 1] - v0) / CELL).astype(int)
    ok = (iu >= 0) & (iu < nu) & (iv >= 0) & (iv < nv) & (K != 2)
    k = iv[ok] * nu + iu[ok]
    top = np.full(nu * nv, -np.inf); np.maximum.at(top, k, P[ok, 2])
    top = top.reshape(nv, nu)
    # colour of the highest point per cell
    order = np.argsort(P[ok, 2]); col = np.zeros((nv * nu, 3), np.uint8); col[k[order]] = C[ok][order]
    col = col.reshape(nv, nu, 3)
    uc, vc = np.meshgrid(u0 + (np.arange(nu) + 0.5) * CELL, v0 + (np.arange(nv) + 0.5) * CELL)
    xy = o + uc[..., None] * ax + vc[..., None] * bx
    inside = contains_xy(target.buffer(0.5), xy[..., 0], xy[..., 1])
    have = np.isfinite(top) & inside
    idx = ndimage.distance_transform_edt(~have, return_distances=False, return_indices=True)
    top = top[tuple(idx)]; col = col[tuple(idx)]
    # isolated spikes (birds, noise): more than 3 m above the 3x3 median
    med = ndimage.median_filter(top, 3)
    top = np.where(top > med + 3, med, top)
    gnd = ground_at(xy[..., 0], xy[..., 1])
    top = np.where(inside, np.maximum(top, gnd + 0.5), gnd)
    return dict(top=top, col=col, inside=inside, gnd=gnd, u0=u0, v0=v0, nu=nu, nv=nv)


def build(d, o, ax, bx):
    """Vertices (EPSG:6677), normals, triangles and the source colour of the LiDAR, as flat arrays."""
    top, inside, gnd, u0, v0 = d["top"], d["inside"], d["gnd"], d["u0"], d["v0"]
    to_xy = lambda u, v: o + np.multiply.outer(u, ax) + np.multiply.outer(v, bx)
    V, N, T, Cl = [], [], [], []
    n = 0
    # tops
    iv, iu = np.nonzero(inside)
    cu = u0 + iu * CELL; cv = v0 + iv * CELL
    corners = [(0, 0), (1, 0), (1, 1), (0, 1)]
    q = np.stack([np.column_stack([to_xy(cu + a * CELL, cv + b * CELL), top[iv, iu]]) for a, b in corners], 1)
    m = len(iu)
    V.append(q.reshape(-1, 3)); N.append(np.tile([0, 0, 1.0], (4 * m, 1)))
    base = np.arange(m)[:, None] * 4
    T.append((base + np.array([[0, 1, 2], [0, 2, 3]]).reshape(1, 6)).reshape(-1, 3))
    Cl.append(np.repeat(d["col"][iv, iu], 4, 0)); n += 4 * m
    # walls: for each inside cell and each of its 4 sides, if the neighbour is lower
    sides = [((1, 0), (1, 0), (1, 1), ax), ((-1, 0), (0, 1), (0, 0), -ax),
             ((0, 1), (1, 1), (0, 1), bx), ((0, -1), (0, 0), (1, 0), -bx)]
    for (du, dv), (a0, b0), (a1, b1), nrm in sides:
        ju, jv = iu + du, iv + dv
        okn = (ju >= 0) & (ju < d["nu"]) & (jv >= 0) & (jv < d["nv"])
        lo = np.where(okn, top[np.clip(jv, 0, d["nv"] - 1), np.clip(ju, 0, d["nu"] - 1)], gnd[iv, iu])
        hi = top[iv, iu]
        w = hi - lo > 0.05
        for i in np.flatnonzero(w):
            rows = max(int(np.ceil((hi[i] - lo[i]) / ROW)), 1)
            z = np.linspace(lo[i], hi[i], rows + 1)
            p0 = to_xy(cu[i] + a0 * CELL, cv[i] + b0 * CELL); p1 = to_xy(cu[i] + a1 * CELL, cv[i] + b1 * CELL)
            ring = np.concatenate([np.column_stack([np.tile(p0, (rows + 1, 1)), z]),
                                   np.column_stack([np.tile(p1, (rows + 1, 1)), z])])
            r = np.arange(rows)
            t = np.concatenate([np.column_stack([r, r + rows + 1, r + rows + 2]), np.column_stack([r, r + rows + 2, r + 1])])
            V.append(ring); N.append(np.tile([*nrm, 0.0], (len(ring), 1))); T.append(t + n)
            Cl.append(np.tile(d["col"][iv[i], iu[i]], (len(ring), 1))); n += len(ring)
    return np.concatenate(V), np.concatenate(N), np.concatenate(T), np.concatenate(Cl)


class Photo:
    def __init__(self, im, cam, path, scale=0.5):
        T = im.cam_from_world()
        self.R, self.t = T.rotation.matrix(), np.asarray(T.translation)
        self.C = np.asarray(im.projection_center())
        self.f, self.cx, self.cy, self.k1 = cam.params[:4]
        self.W, self.H = cam.width, cam.height
        self.rgb = np.asarray(Image.open(path).convert("RGB").resize((cam.width, cam.height)), np.float32)
        self.s = scale                                 # depth buffer at half size
        self.name = im.name

    def project(self, X):
        Xc = X @ self.R.T + self.t
        z = Xc[:, 2]
        x, y = Xc[:, 0] / z, Xc[:, 1] / z
        dd = 1 + self.k1 * (x * x + y * y)
        return self.f * x * dd + self.cx, self.f * y * dd + self.cy, z

    def depth(self, X, size):
        """Nearest depth per pixel of the half-size buffer, each point splatted at its own size (m)."""
        w, h = int(self.W * self.s), int(self.H * self.s)
        u, v, z = self.project(X)
        u, v = u * self.s, v * self.s
        r = np.clip(0.5 * size * self.f * self.s / z, 0.5, 6)
        ok = (z > 1) & (u > -6) & (u < w + 6) & (v > -6) & (v < h + 6)
        u, v, z, r = u[ok], v[ok], z[ok], r[ok]
        buf = np.full(w * h, np.inf)
        R = int(np.ceil(r.max())) if len(r) else 0
        for du in range(-R, R + 1):
            for dv in range(-R, R + 1):
                m = (np.abs(du) <= r) & (np.abs(dv) <= r)
                pu = np.floor(u[m] + du).astype(int); pv = np.floor(v[m] + dv).astype(int)
                k = (pu >= 0) & (pu < w) & (pv >= 0) & (pv < h)
                np.minimum.at(buf, pv[k] * w + pu[k], z[m][k])
        self.buf, self.bw, self.bh = buf.reshape(h, w), w, h

    def sample(self, X, Nrm):
        """Colour, weight and visibility of each vertex in this photo."""
        u, v, z = self.project(X)
        d = self.C - X
        dist = np.linalg.norm(d, axis=1)
        cosang = (Nrm * d).sum(1) / dist
        ok = (z > 1) & (u >= 0) & (u < self.W - 1) & (v >= 0) & (v < self.H - 1) & (cosang > 0.26)
        bu = np.clip((u * self.s).astype(int), 0, self.bw - 1); bv = np.clip((v * self.s).astype(int), 0, self.bh - 1)
        ok &= z <= self.buf[bv, bu] + np.maximum(0.6, 0.01 * z)
        col = np.zeros((len(X), 3), np.float32)
        i = np.flatnonzero(ok)
        u0, v0 = np.floor(u[i]).astype(int), np.floor(v[i]).astype(int)
        fu, fv = (u[i] - u0)[:, None], (v[i] - v0)[:, None]
        im = self.rgb
        col[i] = (im[v0, u0] * (1 - fu) * (1 - fv) + im[v0, u0 + 1] * fu * (1 - fv)
                  + im[v0 + 1, u0] * (1 - fu) * fv + im[v0 + 1, u0 + 1] * fu * fv)
        # leaves: green clearly above red and blue (red brick and grey stone are not)
        green = (2 * col[:, 1] - col[:, 0] - col[:, 2]) / np.maximum(col.sum(1), 1) > 0.08
        w = np.where(ok, cosang / np.sqrt(dist), 0.0) * np.where(green, 0.02, 1.0)
        return col, w, ok   # 1/dist let one close photo with flowers in front win


def wmedian(vals, w):
    """Weighted median along axis 0 (photos) for every vertex and channel. vals: (p, n, 3), w: (p, n)."""
    out = np.zeros(vals.shape[1:], np.float32)
    for c in range(3):
        x = vals[..., c]
        o = np.argsort(x, 0)
        xs = np.take_along_axis(x, o, 0); ws = np.take_along_axis(w, o, 0)
        cw = np.cumsum(ws, 0)
        half = cw[-1] / 2
        k = (cw < half[None]).sum(0)
        out[:, c] = np.take_along_axis(xs, np.clip(k, 0, len(x) - 1)[None], 0)[0]
    return out


def write_ply(path, X, col, src):
    with open(path, "wb") as f:
        f.write((f"ply\nformat binary_little_endian 1.0\ncomment EPSG:6677, metres; source 1 = photos, 0 = LiDAR\n"
                 f"element vertex {len(X)}\nproperty double x\nproperty double y\nproperty double z\n"
                 "property uchar red\nproperty uchar green\nproperty uchar blue\nproperty uchar source\nend_header\n").encode())
        rec = np.zeros(len(X), [("x", "<f8"), ("y", "<f8"), ("z", "<f8"), ("r", "u1"), ("g", "u1"), ("b", "u1"), ("s", "u1")])
        rec["x"], rec["y"], rec["z"] = X.T
        rec["r"], rec["g"], rec["b"] = col.T
        rec["s"] = src
        f.write(rec.tobytes())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model", help="aligned COLMAP model (refine.py output)")
    ap.add_argument("out")
    ap.add_argument("--tiles", nargs="+", required=True)
    ap.add_argument("--center", nargs=2, type=float, required=True)
    ap.add_argument("--half", type=float, default=90)
    ap.add_argument("--photos", default="data/photos/yasuda")
    ap.add_argument("--footprints", default="data/hongo_bldg.geojson")
    ap.add_argument("--name", default="yasuda")
    ap.add_argument("--min-views", type=int, default=2, help="photos needed for a wall colour")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    cx, cy = a.center

    P, K, C = align.lidar(a.tiles, cx, cy, a.half)
    feats = [shape(f["geometry"]) for f in json.load(open(a.footprints))["features"]]
    target = min(feats, key=lambda g: g.distance(Point(cx, cy)))
    x0, y0, gc = cx - a.half, cy - a.half, 0.5
    gn = int(2 * a.half / gc)
    G = align.ground_grid(P, K, x0, y0, gn, gc)
    ground_at = lambda x, y: G[np.clip(((y - y0) / gc).astype(int), 0, gn - 1), np.clip(((x - x0) / gc).astype(int), 0, gn - 1)]
    o, ax, bx = frame(target)
    d = dsm(P, K, C, target, o, ax, bx, ground_at)
    X, Nrm, T, Cl = build(d, o, ax, bx)
    print(f"grid {d['nu']}x{d['nv']} at {CELL} m, axis {np.degrees(np.arctan2(ax[1], ax[0])):.1f} deg; "
          f"{len(X)} vertices, {len(T)} triangles", flush=True)

    # occluders: LiDAR non-ground points near the building but not on it, plus the model's own vertices
    # ground too: the hedges are filed as ground, and walls that hang below the plaza must be hidden by it
    near = contains_xy(target.buffer(60), P[:, 0], P[:, 1]) & ~contains_xy(target.buffer(1.0), P[:, 0], P[:, 1])
    occ = P[near]
    # hedges in front of the walls: the LiDAR files them as ground, so take the photos' own SfM points
    # low (under 3 m) and clear of the wall (over 1.5 m out), each blocking a 1 m disc
    rec = pycolmap.Reconstruction(a.model)
    S = np.array([p.xyz for p in rec.points3D.values()])
    low = (S[:, 2] - ground_at(S[:, 0], S[:, 1]) < 3) & ~contains_xy(target.buffer(1.5), S[:, 0], S[:, 1]) \
        & contains_xy(target.buffer(30), S[:, 0], S[:, 1])
    print(f"{low.sum()} low SfM points in front of the walls used as occluders", flush=True)
    occ_all = np.concatenate([X, occ, S[low]])
    occ_size = np.concatenate([np.full(len(X) + len(occ), CELL), np.full(low.sum(), 1.0)])

    photos = [Photo(im, rec.cameras[im.camera_id], os.path.join(a.photos, im.name)) for im in rec.images.values()]
    vals, ws = [], []
    for k, ph in enumerate(photos):
        ph.depth(occ_all, occ_size)
        c, w, ok = ph.sample(X, Nrm)
        vals.append(c); ws.append(w)
        print(f"  {k + 1}/{len(photos)} {ph.name[:50]}: sees {ok.sum()} vertices", flush=True)
    vals = np.stack(vals); ws = np.stack(ws)
    seen = (ws > 0).sum(0)
    # exposure: per photo and channel, the median ratio to the consensus, twice
    gain = np.ones((len(photos), 3), np.float32)
    for _ in range(2):
        cons = wmedian(vals * gain[:, None], ws)
        for p in range(len(photos)):
            m = (ws[p] > 0) & (seen >= 2)
            if m.sum() > 200:
                gain[p] = np.clip(np.median(cons[m] / np.maximum(vals[p][m] * gain[p], 1), 0) * gain[p], 0.5, 2.0)
    col = np.clip(wmedian(vals * gain[:, None], ws), 0, 255).astype(np.uint8)
    # a wall colour needs a few photos that see it unblocked; one photo through a gap in the hedge is
    # what left the flowers. Walls no photo sees well are not guessed: one flat grey, its own part.
    # Roofs and flat tops keep the LiDAR colour (from the aerial photo, so it is a real observation)
    wall = np.abs(Nrm[:, 2]) < 0.5
    good = (ws > 0.02 * ws.max()).sum(0)
    from_photo = np.where(wall, good >= a.min_views, seen > 0)
    unseen_wall = wall & ~from_photo
    col = np.where(from_photo[:, None], col, Cl)
    col[unseen_wall] = UNSEEN

    # local frame like mesh.py: origin at the building centre, at the ground around it
    z0 = float(np.median(d["gnd"][~d["inside"]]))
    origin = np.array([o[0], o[1], z0])
    L = X - origin
    # split the triangles by where their colour came from (all three vertices from photos or not)
    tp = from_photo[T].all(1)
    tu = unseen_wall[T].any(1) & ~tp
    parts = []
    for name, sel in ((f"{a.name}_photo", tp), (f"{a.name}_lidar_fallback", ~tp & ~tu), (f"{a.name}_unseen_wall", tu)):
        part = Part(name)
        used = np.unique(T[sel])
        remap = np.full(len(X), -1); remap[used] = np.arange(len(used))
        part.add(L[used], col[used], remap[T[sel]], nrm=Nrm[used])
        parts.append(part)
    write_glb(os.path.join(a.out, f"{a.name}.glb"), parts)

    # the "before": LiDAR points of the same area (the building and 10 m around), same axes
    area = contains_xy(target.buffer(10), P[:, 0], P[:, 1])
    write_glb(os.path.join(a.out, f"{a.name}_lidar.glb"), [Cloud(f"{a.name}_lidar_points", P[area] - origin, C[area])])
    write_ply(os.path.join(a.out, f"{a.name}.ply"), X, col, from_photo.astype(np.uint8))

    src = json.load(open(os.path.join(a.photos, "sources.json")))["kept"]
    used = {ph.name for ph, w in zip(photos, ws) if (w > 0).any()}
    meta = {"name": a.name, "crs": "EPSG:6677", "origin_epsg6677": origin.tolist(),
            "axes": "glTF: x east, y up, z south, metres; add origin_epsg6677 to (x, -z, y) for EPSG:6677",
            "label": "Colours added from photos (Wikimedia Commons); shape from aerial LiDAR (Tokyo, CC BY 4.0)",
            "grid_m": CELL, "wall_row_m": ROW, "vertices": int(len(X)), "triangles": int(len(T)),
            "coverage": {"vertices_from_photos": float(from_photo.mean()),
                         "wall_vertices_from_photos": float(from_photo[wall].mean()),
                         "wall_vertices_unseen": float(unseen_wall[wall].mean()), "min_views_on_walls": a.min_views,
                         "photos_used": len(used), "photos_registered": len(photos)},
            "exposure_gain": {ph.name: gain[k].round(3).tolist() for k, ph in enumerate(photos)},
            "sources": [s for s in src if s["file"] in used]}
    with open(os.path.join(a.out, f"{a.name}.json"), "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print(json.dumps(meta["coverage"]), "origin", origin.round(2).tolist())


if __name__ == "__main__":
    main()
