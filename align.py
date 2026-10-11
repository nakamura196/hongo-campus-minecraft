"""Bring the sfm.py model into EPSG:6677 (metres, heights as in the LiDAR) by fitting it to the LiDAR.

    .venv/bin/python align.py out/yasuda_photo/sfm/model out/yasuda_photo/aligned \
        --tiles 09LD0853 09LD0854 09LD0863 09LD0864 --center -6454 -31806 --half 110

The photo model has an unknown scale, rotation and position (7 numbers). No hand-picked control
points are used; the fit uses only the shape of the building:

1. Up: photographers hold cameras without much roll, so the image x-axes of all cameras are nearly
   horizontal; "up" is the direction most perpendicular to all of them.
2. The LiDAR has few points on walls, so walls are filled in from the height model (DSM): where the
   DSM drops by more than a metre between neighbouring 0.5 m cells, a vertical column of points is
   put between the two heights. Together with the real points this is the "surface".
3. From above, every yaw (2 degrees apart) and scale (5 % apart) is tried; for each, the best
   horizontal shift is found by FFT cross-correlation of the photo points' footprint with the
   surface outline. The height shift is then chosen so the points meet the surface best.
4. ICP with scale (Umeyama on the trimmed nearest neighbours) refines all 7 numbers.

Writes <out>/model (COLMAP, EPSG:6677 coordinates), <out>/transform.json (the similarity and the
residuals), <out>/top.png (outline, points and cameras from above) and <out>/check_*.jpg
(LiDAR points drawn into a few photos with the fitted cameras: the honest test of the fit).
"""
import argparse, json, os
import numpy as np, laspy, pycolmap
from scipy import ndimage
from scipy.spatial import cKDTree
from PIL import Image, ImageDraw

np.seterr(all="ignore")   # numpy 2.0 + Accelerate on macOS warns spuriously inside matmul


def lidar(tiles, cx, cy, half):
    P, K, C = [], [], []
    for t in tiles:
        las = laspy.read(f"data/lidar/{t}.las")
        x, y = np.asarray(las.x), np.asarray(las.y)
        m = (np.abs(x - cx) < half) & (np.abs(y - cy) < half)
        P.append(np.column_stack([x[m], y[m], np.asarray(las.z)[m]]))
        K.append(np.asarray(las.classification)[m])
        rgb = np.column_stack([np.asarray(las.red)[m], np.asarray(las.green)[m], np.asarray(las.blue)[m]])
        C.append((rgb >> 8 if rgb.max() > 255 else rgb).astype(np.uint8))
    return np.concatenate(P), np.concatenate(K), np.concatenate(C)


def footprint_mask(path, x0, y0, n, cell, grow=1.0):
    """Cells inside (or within `grow` m of) a building footprint, so tree crowns do not make walls."""
    from shapely.geometry import shape
    img = Image.new("L", (n, n), 0); dr = ImageDraw.Draw(img)
    for f in json.load(open(path))["features"]:
        g = shape(f["geometry"]).buffer(grow)
        for poly in getattr(g, "geoms", [g]):
            dr.polygon([((x - x0) / cell, (y - y0) / cell) for x, y in poly.exterior.coords], fill=1)
    return np.asarray(img, bool)


def _poly_img(g, x0, y0, n, cell):
    img = Image.new("L", (n, n), 0); dr = ImageDraw.Draw(img)
    for poly in getattr(g, "geoms", [g]):
        dr.polygon([((x - x0) / cell, (y - y0) / cell) for x, y in poly.exterior.coords], fill=1)
    return img


def ground_grid(P, K, x0, y0, n, cell):
    """Lowest ground (class 2) point per cell, holes filled from the nearest cell."""
    G = P[K == 2]
    ix = ((G[:, 0] - x0) / cell).astype(int); iy = ((G[:, 1] - y0) / cell).astype(int)
    ok = (ix >= 0) & (ix < n) & (iy >= 0) & (iy < n)
    g = np.full((n, n), np.inf)
    np.minimum.at(g, (iy[ok], ix[ok]), G[ok, 2])
    idx = ndimage.distance_transform_edt(~np.isfinite(g), return_distances=False, return_indices=True)
    return g[tuple(idx)]


def surface(P, x0, y0, n, cell=0.5, step=0.5, drop=1.0, mask=None):
    """DSM on an n x n grid from (x0, y0) (row 0 = south), and wall points where it drops
    (only where the higher side is inside `mask`, if given)."""
    ix = ((P[:, 0] - x0) / cell).astype(int); iy = ((P[:, 1] - y0) / cell).astype(int)
    ok = (ix >= 0) & (ix < n) & (iy >= 0) & (iy < n)
    dsm = np.full((n, n), -np.inf)
    np.maximum.at(dsm, (iy[ok], ix[ok]), P[ok, 2])
    hole = ~np.isfinite(dsm)
    idx = ndimage.distance_transform_edt(hole, return_distances=False, return_indices=True)
    dsm = dsm[tuple(idx)]
    W = []
    for dy, dx in ((0, 1), (1, 0)):
        a, b = dsm[:n - dy, :n - dx], dsm[dy:, dx:]
        wall = np.abs(a - b) > drop
        if mask is not None:
            wall &= np.where(a > b, mask[:n - dy, :n - dx], mask[dy:, dx:])
        jy, jx = np.nonzero(wall)
        lo, hi = np.minimum(a, b)[jy, jx], np.maximum(a, b)[jy, jx]
        px = x0 + (jx + 0.5 + dx / 2) * cell; py = y0 + (jy + 0.5 + dy / 2) * cell
        for k in range(len(jx)):
            z = np.arange(lo[k], hi[k], step)
            W.append(np.column_stack([np.full_like(z, px[k]), np.full_like(z, py[k]), z]))
    return dsm, np.concatenate(W)


def level(rec):
    """Rotation taking the photo frame to one whose z is up (and x, y horizontal)."""
    xs, downs = [], []
    for im in rec.images.values():
        R = im.cam_from_world().rotation.matrix()
        xs.append(R[0]); downs.append(R[1])          # camera x and y axes in world coordinates
    up = np.linalg.svd(np.array(xs))[2][-1]
    if up @ np.mean(downs, 0) > 0:
        up = -up
    a = np.cross(up, [1.0, 0, 0]); a /= np.linalg.norm(a)
    return np.stack([a, np.cross(up, a), up])


def _cams_ok(Cw, tall, ground, x0, y0, n, cell):
    """Share of cameras standing on open ground at a plausible height (0 to 4 m above it)."""
    i = ((Cw[:, 1] - y0) / cell).astype(int); j = ((Cw[:, 0] - x0) / cell).astype(int)
    inn = (i >= 0) & (i < n) & (j >= 0) & (j < n)
    i, j = np.clip(i, 0, n - 1), np.clip(j, 0, n - 1)
    h = Cw[:, 2] - ground[i, j]
    ok = inn & (tall[i, j] < 0.5) & (h > -0.5) & (h < 4)
    return ok.mean(), h


def umeyama(src, dst):
    ms, md = src.mean(0), dst.mean(0)
    S, D = src - ms, dst - md
    U, sig, Vt = np.linalg.svd(D.T @ S / len(src))
    E = np.eye(3); E[2, 2] = np.sign(np.linalg.det(U @ Vt))
    R = U @ E @ Vt
    s = (sig * np.diag(E)).sum() / (S ** 2).sum(1).mean()
    return s, R, md - s * R @ ms


def umeyama_yaw(src, dst):
    """Like umeyama, but the rotation turns about the vertical only (the photo frame is already level)."""
    ms, md = src.mean(0), dst.mean(0)
    S, D = src - ms, dst - md
    th = np.arctan2((S[:, 0] * D[:, 1] - S[:, 1] * D[:, 0]).sum(), (S[:, 0] * D[:, 0] + S[:, 1] * D[:, 1]).sum())
    c, d = np.cos(th), np.sin(th)
    R = np.array([[c, -d, 0], [d, c, 0], [0, 0, 1]])
    s = ((S @ R.T) * D).sum() / (S ** 2).sum()
    return s, R, md - s * R @ ms


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model"); ap.add_argument("out")
    ap.add_argument("--tiles", nargs="+", required=True)
    ap.add_argument("--center", nargs=2, type=float, required=True)
    ap.add_argument("--half", type=float, default=110)
    ap.add_argument("--photos", default="data/photos/yasuda")
    ap.add_argument("--footprints", default="data/hongo_bldg.geojson")
    ap.add_argument("--checks", type=int, default=4)
    ap.add_argument("--init", help="transform.json of an earlier run: skip the coarse search and refine from it")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    cx, cy = a.center

    rec = pycolmap.Reconstruction(a.model)
    pts = [p for p in rec.points3D.values() if p.track.length() >= 3 and p.error < 2.0]
    S = np.array([p.xyz for p in pts])
    cams = np.array([im.projection_center() for im in rec.images.values()])
    B = level(rec)
    S0 = (S - np.median(S, 0)) @ B.T                  # levelled, centred photo points
    C0 = (cams - np.median(S, 0)) @ B.T

    P, K, _ = lidar(a.tiles, cx, cy, a.half)
    cell = 0.5; n = int(2 * a.half / cell); x0, y0 = cx - a.half, cy - a.half
    dsm, W = surface(P, x0, y0, n, cell, mask=footprint_mask(a.footprints, x0, y0, n, cell))
    T = np.concatenate([P, W])
    print(f"{len(S)} photo points, {len(cams)} cameras; LiDAR {len(P)} points + {len(W)} wall points")

    # 3. coarse search from above, against the outline of the target building only
    from shapely.geometry import shape, Point
    feats = [shape(f["geometry"]) for f in json.load(open(a.footprints))["features"]]
    target = min(feats, key=lambda g: g.distance(Point(cx, cy))).buffer(2.0)
    tmask = np.asarray(_poly_img(target, x0, y0, n, cell), bool)
    ground = ground_grid(P, K, x0, y0, n, cell)
    tall = (dsm - ground > 3).astype(float)            # buildings and trees: no camera stands there
    # scale from height: the top of the building over the ground where the photographers stood
    inside = tmask[((P[:, 1] - y0) / cell).astype(int), ((P[:, 0] - x0) / cell).astype(int)]
    z_top = np.percentile(P[inside & (K != 2), 2], 99.9)
    near = np.asarray(_poly_img(target.buffer(40), x0, y0, n, cell), bool) & ~tmask & (dsm - ground < 0.5)
    z_ground = np.median(ground[near])
    base = (z_top - z_ground) / (np.percentile(S0[:, 2], 99.5) - np.median(C0[:, 2]))
    print(f"building top {z_top:.1f} m, ground {z_ground:.1f} m -> scale about {base:.2f}")
    # multi-start ICP: yaw every 15 degrees, the photo points' centre on a 6 m grid over the target;
    # a few cheap iterations each, then the best starts are run to the end
    # thin the surface to one point per 0.3 m voxel near the target (the full set makes ICP slow)
    reg = np.asarray(_poly_img(target.buffer(30), x0, y0, n, cell), bool)
    inr = reg[np.clip(((T[:, 1] - y0) / cell).astype(int), 0, n - 1), np.clip(((T[:, 0] - x0) / cell).astype(int), 0, n - 1)]
    _, first = np.unique(np.floor(T[inr] / 0.3).astype(np.int64), axis=0, return_index=True)
    T = T[inr][first]
    tree = cKDTree(T)
    print(f"surface near the target: {len(T)} points", flush=True)
    keep = np.random.default_rng(0).permutation(len(S0))[:800]
    Q = S0[keep]
    gx0, gy0, gx1, gy1 = target.buffer(10).bounds
    cam_ok = lambda Cw: _cams_ok(Cw, tall, ground, x0, y0, n, cell)

    def icp(s, R, t, iters, Q=Q):
        for _ in range(iters):
            X = s * Q @ R.T + t
            dist, j = tree.query(X, distance_upper_bound=8)
            ok = np.isfinite(dist)
            if ok.sum() < 50:
                return s, R, t, np.inf
            keep = ok & (dist <= np.percentile(dist[ok], 70))
            s2, R2, t2 = umeyama_yaw(X[keep], T[j[keep]])
            s, R, t = s2 * s, R2 @ R, s2 * R2 @ t + t2
        d = tree.query(s * Q @ R.T + t)[0]
        return s, R, t, float(np.median(d))

    if a.init is None:
        starts = []
        for yaw in np.radians(np.arange(0, 360, 15)):
            c, d = np.cos(yaw), np.sin(yaw)
            Rz = np.array([[c, -d, 0], [d, c, 0], [0, 0, 1]])
            tz = z_ground + 1.6 - base * np.median(C0[:, 2])
            for gx in np.arange(gx0, gx1, 6.0):
                for gy in np.arange(gy0, gy1, 6.0):
                    starts.append(icp(base, Rz, np.array([gx, gy, tz]), 6))
        print(f"{len(starts)} starts done", flush=True)
        starts.sort(key=lambda r: r[3])
        results = []
        for s_, R_, t_, _ in starts[:40]:
            s_, R_, t_, med = icp(s_, R_, t_, 40)
            if abs(np.log(s_ / base)) > np.log(1.3):
                continue
            frac, h = cam_ok(s_ * C0 @ R_.T + t_)
            results.append((med + 2.0 * (1 - frac), med, frac, s_, R_, t_))
        results.sort(key=lambda r: r[0])
        for r in results[:5]:
            yaw_r = np.degrees(np.arctan2(r[4][1, 0], r[4][0, 0])) % 360
            print(f"  start: median {r[1]:.2f} m, cameras on open ground {r[2]:.0%}, scale {r[3]:.2f}, yaw {yaw_r:.0f}")
        _, _, _, s, Rl, tl = results[0]
    else:
        j0 = json.load(open(a.init))
        Rl = np.array(j0["rotation"]) @ B.T                # back to the levelled frame, keeping the yaw only
        th = np.arctan2(Rl[1, 0], Rl[0, 0]); c, d = np.cos(th), np.sin(th)
        Rl = np.array([[c, -d, 0], [d, c, 0], [0, 0, 1]])
        s = j0["scale"]; tl = s * np.array(j0["rotation"]) @ np.median(S, 0) + np.array(j0["translation"])
    R = Rl @ B; t = tl - s * R @ np.median(S, 0)

    # 4. ICP with scale, trimmed; the rotation turns about the vertical only (free rotation tilted the model 5 degrees)
    for it in range(60):
        X = s * S @ R.T + t
        dist, j = tree.query(X)
        keep = dist < np.percentile(dist, 70)
        s2, R2, t2 = umeyama_yaw(X[keep], T[j[keep]])
        s, R, t = s2 * s, R2 @ R, s2 * R2 @ t + t2
        if abs(s2 - 1) < 1e-6 and np.abs(t2).max() < 1e-4:
            break
    X = s * S @ R.T + t
    dist = tree.query(X)[0]
    camsX = s * cams @ R.T + t
    gi = np.clip(((camsX[:, 1] - y0) / cell).astype(int), 0, n - 1); gj = np.clip(((camsX[:, 0] - x0) / cell).astype(int), 0, n - 1)
    cam_h = camsX[:, 2] - ground[gi, gj]
    res = {"scale": s, "rotation": R.tolist(), "translation": t.tolist(), "icp_iterations": it + 1,
           "photo_points": len(S), "distance_m": {"median": float(np.median(dist)), "p75": float(np.percentile(dist, 75)),
                                                  "within_0.5m": float((dist < 0.5).mean()), "within_1m": float((dist < 1).mean())},
           "camera_height_above_ground_m": {"median": float(np.median(cam_h)), "min": float(cam_h.min()), "max": float(cam_h.max())},
           "tilt_deg_after_icp": float(np.degrees(np.arccos(np.clip(R[2] @ B[2], -1, 1))))}
    print(json.dumps(res["distance_m"]), json.dumps(res["camera_height_above_ground_m"]))
    with open(os.path.join(a.out, "transform.json"), "w") as f:
        json.dump(res, f, indent=1)

    sim = pycolmap.Sim3d(s, pycolmap.Rotation3d(R), t)
    rec.transform(sim)
    os.makedirs(os.path.join(a.out, "model"), exist_ok=True)
    rec.write(os.path.join(a.out, "model"))

    # pictures: from above, and LiDAR drawn into photos
    g = 3
    top = Image.fromarray((255 - tall * 60 - tmask * 120).astype(np.uint8)[::-1]).convert("RGB")
    top = top.resize((n * g // 2, n * g // 2)); dr = ImageDraw.Draw(top)
    f = g / 2 / cell
    for x, y, _ in X[::2]:
        dr.point(((x - x0) * f, (n * cell - (y - y0)) * f), (200, 40, 40))
    for x, y, _ in camsX:
        u, v = (x - x0) * f, (n * cell - (y - y0)) * f
        dr.ellipse((u - 4, v - 4, u + 4, v + 4), outline=(0, 90, 220), width=2)
    top.save(os.path.join(a.out, "top.png"))
    imgs = sorted(rec.images.values(), key=lambda im: -im.num_points3D)[:a.checks]
    sel = P[(K != 2)][::3]
    for im in imgs:
        photo = Image.open(os.path.join(a.photos, im.name)).convert("RGB"); dr = ImageDraw.Draw(photo)
        cam = rec.cameras[im.camera_id]
        Xc = sel @ im.cam_from_world().rotation.matrix().T + im.cam_from_world().translation
        front = Xc[:, 2] > 1
        uv = np.array(cam.img_from_cam(Xc[front]))
        z = Xc[front, 2]
        for (u, v), zz in zip(uv, z):
            if 0 <= u < cam.width and 0 <= v < cam.height:
                h = int(np.clip((zz - 20) / 120 * 255, 0, 255))
                dr.point((u, v), (255, 255 - h, h))
        photo.save(os.path.join(a.out, f"check_{im.image_id}.jpg"), quality=85)


if __name__ == "__main__":
    main()
