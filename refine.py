"""Refine an aligned photo model so that the skyline drawn from LiDAR matches the skyline in each photo.

    .venv/bin/python refine.py out/yasuda_photo/aligned_yaw out/yasuda_photo/refined --tiles 09LD0853 09LD0854 09LD0863 09LD0864 --center -6454 -31806

align.py brings the photo model close (point-to-surface distance), but aerial LiDAR has almost no
wall points, so the height and the distance of the cameras stay loose: the tower top drawn into the
photos sits metres above the real one. The skyline (where the buildings and trees meet the sky) is
sharp in both sources, so it pins those down.

- LiDAR skyline: the highest non-ground point of every 0.25 m cell, splatted into each photo at the
  cell's size; the topmost splat in every image column is the skyline. Ground points never reach
  above the horizon, so they are left out.
- Photo skyline: the sky colour is learnt from the pixels well above the LiDAR skyline; scanning
  each column down from the top, the skyline is the first run of pixels that are not sky-coloured.
- Only columns whose LiDAR skyline is on the target building are compared. The cameras' poses
  relative to one another come from SfM and stay fixed; only the 7 numbers of the global similarity
  move (scale, rotation, translation), minimising a robust sum of the angular gaps.
- The camera height above the ground is NOT used; it is reported as an independent check.

Writes <out>/transform.json (the full similarity from the SfM frame), <out>/model and
<out>/sky_<id>.jpg (photo skyline red, LiDAR skyline green).
"""
import argparse, json, os
import numpy as np, pycolmap
from PIL import Image, ImageDraw
from scipy.optimize import minimize
from scipy.spatial.transform import Rotation
from shapely.geometry import shape, Point
from shapely import contains_xy
import align

np.seterr(all="ignore")
W_HALF = 800                                           # photos are compared at this width
SCALES = [0.03, 0.01, 0.004]                           # robust scale per round (rad): coarse to fine


def cells(P, K, x0, y0, n, cell, target):
    """Top of every non-ground cell: (x, y, z) at the cell centre, and whether it is on the target."""
    Q = P[K != 2]
    i = ((Q[:, 1] - y0) / cell).astype(int); j = ((Q[:, 0] - x0) / cell).astype(int)
    ok = (i >= 0) & (i < n) & (j >= 0) & (j < n)
    top = np.full(n * n, -np.inf)
    np.maximum.at(top, i[ok] * n + j[ok], Q[ok, 2])
    k = np.flatnonzero(np.isfinite(top))
    X = np.stack([x0 + (k % n + 0.5) * cell, y0 + (k // n + 0.5) * cell, top[k]], 1)
    return X, contains_xy(target, X[:, 0], X[:, 1])


class View:
    def __init__(self, im, cam, photo_dir):
        self.id, self.name = im.image_id, im.name
        T = im.cam_from_world()
        self.R, self.t = T.rotation.matrix(), np.asarray(T.translation)
        k = W_HALF / cam.width
        self.f, self.cx, self.cy = cam.params[0] * k, cam.params[1] * k, cam.params[2] * k
        self.k1 = cam.params[3]
        self.w, self.h = W_HALF, int(round(cam.height * k))
        img = Image.open(os.path.join(photo_dir, im.name)).convert("RGB").resize((self.w, self.h), Image.BILINEAR)
        self.rgb = np.asarray(img, np.float32)
        self.photo_sky = None

    def project(self, X):
        Xc = X @ self.R.T + self.t
        z = Xc[:, 2]
        x, y = Xc[:, 0] / z, Xc[:, 1] / z
        d = 1 + self.k1 * (x * x + y * y)
        return self.f * x * d + self.cx, self.f * y * d + self.cy, z

    def skyline(self, X, tgt, cell):
        """Topmost LiDAR pixel per column (v, or +inf where nothing), and whether it is on the target."""
        u, v, z = self.project(X)
        ok = (z > 2) & (u > -50) & (u < self.w + 50) & (v < self.h)
        u, v, z, tg = u[ok], v[ok], z[ok], tgt[ok]
        r = np.clip(0.5 * cell * self.f / z, 0.5, 12)
        top = v - r
        env = np.full(self.w, np.inf); who = np.zeros(self.w, bool)
        lo = np.floor(u - r).astype(int); hi = np.ceil(u + r).astype(int)
        for k in range(int(np.ceil(r.max())) * 2 + 2):
            c = lo + k
            m = (c <= hi) & (c >= 0) & (c < self.w)
            if not m.any():
                break
            np.minimum.at(env, c[m], top[m])
        # who: is the topmost splat in that column on the target? (second pass, ties count)
        for k in range(int(np.ceil(r.max())) * 2 + 2):
            c = lo + k
            m = (c <= hi) & (c >= 0) & (c < self.w)
            if not m.any():
                break
            hit = m.copy(); hit[m] = top[m] <= env[c[m]] + 1e-6
            who[c[hit & tg]] = True
        return env, who

    def find_photo_sky(self, env):
        """Sky colour from pixels at least 25 px above the LiDAR skyline; then the first non-sky run per column."""
        rows = np.arange(self.h)[:, None]
        above = rows < (env[None, :] - 25)
        px = self.rgb[above]
        if len(px) < 2000:
            self.photo_sky = None; return
        med = np.median(px, 0)
        spread = np.median(np.abs(px - med), 0) * 1.4826 + 4
        dist = np.sqrt((((self.rgb - med) / spread) ** 2).sum(2))
        notsky = dist > 6
        # a run of 4 non-sky pixels; the first row where the run starts
        run = notsky[:-3] & notsky[1:-2] & notsky[2:-1] & notsky[3:]
        first = np.where(run.any(0), run.argmax(0), self.h).astype(float)
        first[first == 0] = np.nan                         # building cut by the top edge: no skyline
        self.photo_sky = first
        self.sky_share = float(above.mean())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("aligned", help="output folder of align.py (transform.json and model/)")
    ap.add_argument("out")
    ap.add_argument("--sfm", default="out/yasuda_photo/sfm/model", help="the SfM model in its own frame")
    ap.add_argument("--tiles", nargs="+", required=True)
    ap.add_argument("--center", nargs=2, type=float, required=True)
    ap.add_argument("--half", type=float, default=90)
    ap.add_argument("--cell", type=float, default=0.25)
    ap.add_argument("--photos", default="data/photos/yasuda")
    ap.add_argument("--footprints", default="data/hongo_bldg.geojson")
    ap.add_argument("--rounds", type=int, default=3)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    cx, cy = a.center

    P, K, _ = align.lidar(a.tiles, cx, cy, a.half)
    feats = [shape(f["geometry"]) for f in json.load(open(a.footprints))["features"]]
    target = min(feats, key=lambda g: g.distance(Point(cx, cy)))
    n = int(2 * a.half / a.cell); x0, y0 = cx - a.half, cy - a.half
    X, tgt = cells(P, K, x0, y0, n, a.cell, target)
    ground = align.ground_grid(P, K, x0, y0, int(2 * a.half / 0.5), 0.5)
    c0 = np.array([*target.centroid.coords[0], np.percentile(X[tgt, 2], 50)])
    print(f"{len(X)} LiDAR cells ({tgt.sum()} on the target)", flush=True)

    rec = pycolmap.Reconstruction(os.path.join(a.aligned, "model"))
    views = [View(im, rec.cameras[im.camera_id], a.photos) for im in rec.images.values()]
    centres = np.array([im.projection_center() for im in rec.images.values()])

    def moved(p):
        """LiDAR points moved by the inverse of the correction p (so the cameras can stay put)."""
        R = Rotation.from_rotvec(p[:3]).as_matrix()
        return ((X - c0 - p[4:7]) @ R) / np.exp(p[3]) + c0

    def cost(p, detail=False):
        sc = scale[0]; rho_max = float(np.log1p(64.0))
        Xm = moved(p)
        tot, cnt, per = 0.0, 0, {}
        for vw in views:
            if vw.photo_sky is None:
                continue
            env, who = vw.skyline(Xm, tgt, a.cell)
            m = who & np.isfinite(env) & np.isfinite(vw.photo_sky) & (env > 2)
            e = (env[m] - vw.photo_sky[m]) / vw.f          # radians
            rho = np.minimum(np.log1p((e / sc) ** 2), rho_max)      # Cauchy, capped at 8 scales
            # columns lost since the round began count as the worst gap (no escaping the photos)
            lost = max(vw.n0 - m.sum(), 0)
            tot += (rho.sum() + rho_max * lost) / max(vw.n0, 1); cnt += 1
            if m.sum():
                per[vw.id] = (float(np.median(np.abs(e))), int(m.sum()))
        out = tot / max(cnt, 1)
        return (out, per) if detail else out

    p = np.zeros(7)
    scale = [SCALES[0]]
    for rnd in range(a.rounds):
        scale[0] = SCALES[min(rnd, len(SCALES) - 1)]
        Xm = moved(p)
        for vw in views:
            env, who = vw.skyline(Xm, tgt, a.cell)
            vw.find_photo_sky(env)
            if vw.photo_sky is not None:
                vw.n0 = int((who & np.isfinite(env) & np.isfinite(vw.photo_sky) & (env > 2)).sum())
                if vw.n0 < 20:
                    vw.photo_sky = None
        c, per = cost(p, True)
        print(f"round {rnd}: cost {c:.3f}, {len(per)} photos, median gap "
              f"{np.median([v[0] for v in per.values()]) * 1000:.1f} mrad", flush=True)
        # coarse-to-fine: translation steps of metres, rotation of degrees
        res = minimize(cost, p, method="Powell",
                       options={"xtol": 1e-4, "ftol": 1e-4, "maxfev": 1500,
                                "direc": np.diag([0.02, 0.02, 0.02, 0.02, 1.0, 1.0, 1.0])})
        p = res.x
        print(f"  -> cost {res.fun:.3f} after {res.nfev} evaluations; rot {np.degrees(np.linalg.norm(p[:3])):.2f} deg, "
              f"scale x{np.exp(p[3]):.3f}, shift {np.round(p[4:7], 2).tolist()} m", flush=True)

    if a.rounds == 0:                                  # just look at the skylines
        for vw in views:
            vw.find_photo_sky(vw.skyline(moved(p), tgt, a.cell)[0]); vw.n0 = 1
    c, per = cost(p, True)
    # compose: corrected world = c0 + e^ls R (Y - c0) + dt, with Y = s R0 S + t0
    j0 = json.load(open(os.path.join(a.aligned, "transform.json")))
    R = Rotation.from_rotvec(p[:3]).as_matrix(); sc = np.exp(p[3])
    s1 = sc * j0["scale"]; R1 = R @ np.array(j0["rotation"])
    t1 = c0 + sc * R @ (np.array(j0["translation"]) - c0) + p[4:7]
    sim = pycolmap.Sim3d(sc, pycolmap.Rotation3d(R), c0 - sc * R @ c0 + p[4:7])
    rec.transform(sim)
    newc = np.array([im.projection_center() for im in rec.images.values()])
    gi = np.clip(((newc[:, 1] - y0) / 0.5).astype(int), 0, ground.shape[0] - 1)
    gj = np.clip(((newc[:, 0] - x0) / 0.5).astype(int), 0, ground.shape[1] - 1)
    h = newc[:, 2] - ground[gi, gj]
    oldh = centres[:, 2] - ground[np.clip(((centres[:, 1] - y0) / 0.5).astype(int), 0, ground.shape[0] - 1),
                                  np.clip(((centres[:, 0] - x0) / 0.5).astype(int), 0, ground.shape[1] - 1)]
    gaps = np.array([v[0] for v in per.values()])
    up = R1[2] / np.linalg.norm(R1[2])
    res = {"scale": s1, "rotation": R1.tolist(), "translation": t1.tolist(), "correction": p.tolist(),
           "skyline": {"photos": len(per), "median_gap_mrad": float(np.median(gaps) * 1000),
                       "per_photo": {str(k): {"median_gap_mrad": v[0] * 1000, "columns": v[1]} for k, v in per.items()}},
           "camera_height_above_ground_m": {"median": float(np.median(h)), "p10": float(np.percentile(h, 10)),
                                            "p90": float(np.percentile(h, 90)), "before_median": float(np.median(oldh))}}
    print(json.dumps(res["skyline"]["median_gap_mrad"]), json.dumps(res["camera_height_above_ground_m"]))
    with open(os.path.join(a.out, "transform.json"), "w") as f:
        json.dump(res, f, indent=1)
    os.makedirs(os.path.join(a.out, "model"), exist_ok=True)
    rec.write(os.path.join(a.out, "model"))

    # pictures: photo skyline red, LiDAR skyline green (green only where it is the target)
    for vw in views:
        if vw.photo_sky is None:
            continue
        env, who = vw.skyline(moved(p), tgt, a.cell)
        img = Image.fromarray(vw.rgb.astype(np.uint8)); dr = ImageDraw.Draw(img)
        for u in range(vw.w):
            if np.isfinite(vw.photo_sky[u]):
                dr.point((u, vw.photo_sky[u]), (255, 0, 0))
            if np.isfinite(env[u]):
                dr.point((u, env[u]), (0, 255, 0) if who[u] else (0, 120, 255))
        img.save(os.path.join(a.out, f"sky_{vw.id}.jpg"), quality=85)


if __name__ == "__main__":
    main()
