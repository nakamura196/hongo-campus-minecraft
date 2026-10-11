"""Turn the cells and the LiDAR points into a textured mesh (glTF binary, .glb) for Unity / VRChat.

    python mesh.py CELLS.npz BLDG.geojson OUT.glb --rows R0 R1 --cols C0 C1

The shapes come from the same cleaned cells as build.py (roofs.regularise, trees.separate);
the colours come from the raw points (points.load), not from the 1 m cells:
  - ground: one height field through the cell centres, smoothed (SMOOTH m), textured with an image
    seen from above (TOP m per pixel) made of the lowest ground-class point in each pixel
  - buildings: walls stand on the PLATEAU outline itself; one roof facet per group of cells of
    similar height. Roofs take the image from above made of the highest point in each pixel;
    walls take strips coloured by the points that hit them (WallTex)
  - trees: the points themselves (glTF POINTS), not a mesh

Axes: glTF is y up; x points east and z south, 1 unit = 1 m. The origin is the
centre of the window, at the height of its lowest ground.
"""
import argparse, io, itertools, json, struct
import numpy as np
from scipy.spatial import Delaunay, cKDTree
from scipy import ndimage as ndi
import shapely, shapely.affinity
from shapely.geometry import shape, box

from roofs import regularise, _facets
from trees import separate
import points

GROUND, BUILDING, TREE, OTHER, WATER, LOW = 0, 1, 2, 3, 4, 5
WATER_RGB = (60, 90, 110)
TOP = 0.25        # m per pixel of the ground and roof images
SMOOTH = 1.5      # m, sigma of the blur on the ground heights


def topdown(P, C, keep, span, res, highest):
    """Image seen from above at `res` m per pixel: the colour of the highest (or lowest) kept point
    in each pixel; empty pixels are filled from around (fill_holes). span = (west, north, width, height) in m."""
    w0, n1, Wm, Hm = span
    w, h = int(np.ceil(Wm / res)), int(np.ceil(Hm / res))
    c = ((P[keep, 0] - w0) / res).astype(int)
    r = ((n1 - P[keep, 1]) / res).astype(int)
    z, col = P[keep, 2], C[keep]
    ok = (c >= 0) & (c < w) & (r >= 0) & (r < h)
    c, r, z, col = c[ok], r[ok], z[ok], col[ok]
    key = r * w + c
    order = np.lexsort((z if highest else -z, key))     # within a pixel, the wanted point comes last
    last = np.r_[key[order][1:] != key[order][:-1], True]
    img = np.zeros((h * w, 3), np.uint8)
    have = np.zeros(h * w, bool)
    img[key[order][last]] = col[order][last]
    have[key[order][last]] = True
    return fill_holes(img.reshape(h, w, 3), have.reshape(h, w)), have.mean()


def fill_holes(img, have):
    """Fill the empty pixels with a blur of the filled ones, widening the blur until all are reached
    (copying the nearest pixel instead leaves sharp-edged patches under the trees)."""
    out = img.astype(float)
    done = have.copy()
    s = 1.0
    while not done.all() and s < 256:
        w = ndi.gaussian_filter(have.astype(float), s)
        b = np.stack([ndi.gaussian_filter(np.where(have, out[..., c], 0), s) for c in range(3)], -1)
        new = ~done & (w > 0.02)
        out[new] = b[new] / w[new][:, None]
        done |= new
        s *= 2
    return out.clip(0, 255).astype(np.uint8)


class WallTex:
    """Walls unrolled into strips (along the outline x height, WRES m per pixel) and coloured by the
    LiDAR points that hit them; the strips are packed into one atlas at the end (finish)."""
    WRES = 0.5     # m per pixel
    NEAR = 0.6     # m from the outline for a point to count as a hit on the wall
    PAD = 1        # px around each strip so neighbours do not bleed in

    def __init__(self, P, C):
        g = C.astype(float)
        exg = (2 * g[:, 1] - g[:, 0] - g[:, 2]) / np.maximum(g.sum(1), 1)
        keep = exg <= 0.05                                  # leaves in front of the wall are not wall
        self.P, self.C = P[keep], C[keep]
        self.kd = cKDTree(self.P[:, :2])
        self.strips, self.uses = [], []
        self.hit = self.px = 0

    def strip(self, ring, base, top, fallback):
        """Closed ring (n, 2), wall foot and head heights per ring point. Returns the uv (pixels in the
        strip) of the 4 corners of each segment, in the order building() lays them."""
        n = len(ring)
        nxt = np.roll(np.arange(n), -1)
        seg = np.linalg.norm(ring[nxt] - ring, axis=1)
        s0 = np.r_[0, np.cumsum(seg)[:-1]]
        s1 = s0 + seg
        S, zlo, zhi = s1[-1], base.min(), top.max()
        R = self.WRES
        w, h = int(np.ceil(S / R)) + 1, int(np.ceil((zhi - zlo) / R)) + 1
        acc = np.zeros((h, w, 3))
        cnt = np.zeros((h, w))
        line = shapely.LinearRing(ring)
        dense = np.asarray(shapely.segmentize(line, R).coords)
        near = self.kd.query_ball_point(dense, self.NEAR + R)
        idx = np.unique(np.fromiter(itertools.chain.from_iterable(near), int))
        if len(idx):
            p = self.P[idx]
            pts = shapely.points(p[:, 0], p[:, 1])
            on = shapely.distance(line, pts) <= self.NEAR
            sp = shapely.line_locate_point(shapely.LineString(np.vstack([ring, ring[:1]])), pts[on])
            zp = p[on, 2]
            k = np.clip(np.searchsorted(s1, sp), 0, n - 1)  # segment of each point: wall head there
            t = np.clip((sp - s0[k]) / np.maximum(seg[k], 1e-9), 0, 1)
            head = top[k] + t * (top[nxt][k] - top[k])
            foot = base[k] + t * (base[nxt][k] - base[k])
            wall = (zp > foot + 0.3) & (zp < head - 0.5)    # not the ground, not the roof edge
            u = np.clip((sp[wall] / R).astype(int), 0, w - 1)
            v = np.clip(((zhi - zp[wall]) / R).astype(int), 0, h - 1)
            np.add.at(acc, (v, u), self.C[idx][on][wall].astype(float))
            np.add.at(cnt, (v, u), 1)
        have = cnt > 0
        self.hit += int(have.sum()); self.px += w * h
        if have.any():
            img = acc / np.maximum(cnt, 1)[..., None]
            # fill: blur what was hit (weighted), then the nearest filled pixel for what is still far away
            wsum = ndi.gaussian_filter(have.astype(float), 1.0)
            blur = np.stack([ndi.gaussian_filter(np.where(have, img[..., c], 0), 1.0) for c in range(3)], -1)
            fill = wsum > 0.05
            img = np.where(have[..., None], img, blur / np.maximum(wsum, 1e-9)[..., None])
            _, (iy, ix) = ndi.distance_transform_edt(~(have | fill), return_indices=True)
            img = img[iy, ix]
        else:
            img = np.broadcast_to(np.asarray(fallback, float), (h, w, 3))
        self.strips.append(img.clip(0, 255).astype(np.uint8))
        us = np.stack([s0, s1, s1, s0], 1) / R
        vs = (zhi - np.stack([base, base[nxt], top[nxt], top], 1)) / R
        return np.stack([us, vs], -1).reshape(-1, 2), len(self.strips) - 1

    def finish(self, walls, width=2048):
        """Pack the strips (shelves, tallest first) into one image and turn the walls' pixel uv into 0..1."""
        from PIL import Image
        P = self.PAD
        order = sorted(range(len(self.strips)), key=lambda i: -self.strips[i].shape[0])
        off, x, y, shelf = {}, 0, 0, 0
        for i in order:
            h, w = self.strips[i].shape[:2]
            if x + w + 2 * P > width:
                x, y, shelf = 0, y + shelf, 0
            off[i] = (x + P, y + P)
            x += w + 2 * P
            shelf = max(shelf, h + 2 * P)
        H = int(np.ceil((y + shelf) / 4) * 4)
        atlas = np.zeros((H, width, 3), np.uint8)
        for i, img in enumerate(self.strips):
            ox, oy = off[i]
            h, w = img.shape[:2]
            atlas[oy - P:oy + h + P, ox - P:ox + w + P] = np.pad(img, ((P, P), (P, P), (0, 0)), mode="edge")
        for k, (uv, i) in enumerate(self.uses):
            ox, oy = off[i]
            walls.uv[k] = np.column_stack([(uv[:, 0] + ox) / width, (uv[:, 1] + oy) / H])
        walls.tex = Image.fromarray(atlas)
        return atlas.shape


class Cloud:
    """A glTF POINTS primitive: positions (east, north, up) and 8-bit colours."""

    def __init__(self, name, pos, col):
        self.name, self.pos, self.col, self.n = name, pos, col, len(pos)


def vertex_normals(pos, tri):
    n = np.zeros_like(pos)
    fn = np.cross(pos[tri[:, 1]] - pos[tri[:, 0]], pos[tri[:, 2]] - pos[tri[:, 0]])
    for k in range(3):
        np.add.at(n, tri[:, k], fn)
    return n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-9)


class Part:
    """Vertices (east, north, up in the local frame), colours and triangles of one material.
    With a texture (tex, a PIL image) the vertices carry uv instead of a colour."""

    def __init__(self, name, tex=None, uvmap=None):
        self.name, self.pos, self.col, self.tri, self.nrm, self.uv, self.n = name, [], [], [], [], [], 0
        self.tex, self.uvmap = tex, uvmap

    def add(self, pos, col, tri, nrm=None, uv=None):
        pos = np.asarray(pos, float)
        self.pos.append(pos)
        self.col.append(np.broadcast_to(np.asarray(col, np.uint8), (len(pos), 3)))
        self.tri.append(np.asarray(tri, np.int64) + self.n)
        self.nrm.append(nrm)
        self.uv.append(self.uvmap(pos) if uv is None and self.uvmap else uv)
        self.n += len(pos)

    def arrays(self):
        pos, tri = np.concatenate(self.pos), np.concatenate(self.tri)
        smooth = vertex_normals(pos, tri)
        off, nrm = 0, []
        for p, n in zip(self.pos, self.nrm):
            nrm.append(smooth[off:off + len(p)] if n is None else n)
            off += len(p)
        # local (east, north, up) -> glTF (x east, y up, z south)
        swap = lambda a: np.stack([a[:, 0], a[:, 2], -a[:, 1]], 1)
        return (swap(pos).astype(np.float32), swap(np.concatenate(nrm)).astype(np.float32),
                np.concatenate(self.col), tri.astype(np.uint32))


def terrain(part, ground, col, xs, ys):
    H, W = ground.shape
    gx, gy = np.meshgrid(xs, ys)
    pos = np.stack([gx, gy, ground], -1).reshape(-1, 3)
    i = np.arange(H * W).reshape(H, W)
    a, b, c, d = i[:-1, :-1], i[:-1, 1:], i[1:, :-1], i[1:, 1:]
    # rows run south, so (a, c, b) is counter-clockwise seen from above
    tri = np.concatenate([np.stack([a, c, b], -1).reshape(-1, 3), np.stack([b, c, d], -1).reshape(-1, 3)])
    part.add(pos, col.reshape(-1, 3), tri)


def ring_points(ring, step):
    """Outline resampled every `step` m, without the closing duplicate, counter-clockwise."""
    r = shapely.segmentize(ring, step)
    p = np.asarray(r.coords)[:-1, :2]
    area = 0.5 * np.sum(p[:, 0] * np.roll(p[:, 1], -1) - np.roll(p[:, 0], -1) * p[:, 1])
    return p if area > 0 else p[::-1]


def facet_regions(poly, cells_xy, seg, z, cell):
    """Cut a building outline into the parts covered by each roof facet.

    Facets (roofs._facets: neighbours within STEP m) are outlined by the union of
    their cells, simplified so the cell staircase becomes a straight or diagonal
    edge, and clipped to the outline. Going from the highest facet down, each
    keeps what no higher facet covers, so the parts tile the outline without gaps
    and every step between facets becomes a vertical wall. The lowest facet takes
    the whole remaining outline."""
    ids = [i for i in np.unique(seg) if i >= 0]
    ids.sort(key=lambda i: -np.median(z[seg == i]))
    out, above = [], None
    for k, i in enumerate(ids):
        sel = seg == i
        if k == len(ids) - 1:
            shp = poly
        else:
            sq = shapely.box(cells_xy[sel, 0] - cell / 2, cells_xy[sel, 1] - cell / 2,
                             cells_xy[sel, 0] + cell / 2, cells_xy[sel, 1] + cell / 2)
            shp = shapely.union_all(sq).simplify(0.6 * cell).buffer(0).intersection(poly)
        region = shp if above is None else shp.difference(above)
        above = shp if above is None else shapely.union(above, shp)
        for part in getattr(region, "geoms", [region]):
            if part.geom_type == "Polygon" and part.area >= cell * cell:
                out.append((part, sel))
    return out


def building(roofs, walls, wt, poly, pts_xy, pts_z, pts_rgb, ground_at, step, wall_rgb):
    """Roof through the outline and the inner cell centres; walls down the outline."""
    tree = cKDTree(pts_xy)
    rings = [ring_points(poly.exterior, step)] + [ring_points(r, step)[::-1] for r in poly.interiors]
    edge = np.concatenate(rings)
    _, near = tree.query(edge)
    inner = shapely.contains_xy(poly.buffer(-0.4 * step), pts_xy[:, 0], pts_xy[:, 1])
    xy = np.concatenate([edge, pts_xy[inner]])
    z = np.concatenate([pts_z[near], pts_z[inner]])
    rgb = np.concatenate([pts_rgb[near], pts_rgb[inner]])
    if len(xy) < 3:
        return 0
    try:
        tri = Delaunay(xy).simplices
    except Exception:
        return 0
    cen = xy[tri].mean(1)
    tri = tri[shapely.contains_xy(poly, cen[:, 0], cen[:, 1])]
    # Delaunay gives either winding; make every roof triangle face up
    a, b, c = (xy[tri[:, k]] for k in range(3))
    flip = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0]) < 0
    tri[flip] = tri[flip][:, [0, 2, 1]]
    roofs.add(np.column_stack([xy, z]), rgb, tri)

    off = 0
    for ring in rings:
        n = len(ring)
        top = z[off:off + n]
        base = ground_at(ring) - 0.5
        nxt = np.roll(np.arange(n), -1)
        p0, p1 = ring, ring[nxt]
        # four corners per segment so each wall face is flat-shaded on its own
        quad = np.stack([np.column_stack([p0, base]), np.column_stack([p1, base[nxt]]),
                         np.column_stack([p1, top[nxt]]), np.column_stack([p0, top])], 1).reshape(-1, 3)
        d = p1 - p0
        nrm = np.column_stack([d[:, 1], -d[:, 0], np.zeros(n)])   # outward for a counter-clockwise ring
        nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-9)
        k = np.arange(n)[:, None] * 4
        uv, sid = wt.strip(ring, base, top, wall_rgb)
        walls.add(quad, wall_rgb, np.concatenate([k + [0, 1, 2], k + [0, 2, 3]]), np.repeat(nrm, 4, 0), uv)
        wt.uses.append((uv, sid))
        off += n
    return len(tri)


def write_glb(path, parts):
    """Meshes (Part; with a texture: TEXCOORD_0 + an embedded JPEG, unlit, no COLOR_0) and point clouds (Cloud)."""
    from PIL import Image
    bin_, views, accs, nodes, meshes, mats, imgs, texs = bytearray(), [], [], [], [], [], [], []

    def view(data, target=None):
        while len(bin_) % 4:
            bin_.append(0)
        views.append({"buffer": 0, "byteOffset": len(bin_), "byteLength": len(data)})
        if target:
            views[-1]["target"] = target
        bin_.extend(data)
        return len(views) - 1

    def acc(arr, ctype, typ, target=34962, **kw):
        accs.append({"bufferView": view(arr.tobytes(), target), "componentType": ctype, "count": len(arr),
                     "type": typ, **kw})
        return len(accs) - 1

    swap = lambda a: np.stack([a[:, 0], a[:, 2], -a[:, 1]], 1).astype(np.float32)
    for p in parts:
        if isinstance(p, Cloud):
            pos = swap(p.pos)
            lin = np.round((p.col / 255.0) ** 2.2 * 65535).astype(np.uint16)    # linear, like points.py
            att = {"POSITION": acc(pos, 5126, "VEC3", min=pos.min(0).tolist(), max=pos.max(0).tolist()),
                   "COLOR_0": acc(np.column_stack([lin, np.full(len(lin), 65535, np.uint16)]), 5123, "VEC4",
                                  normalized=True)}
            mats.append({"name": p.name, "extensions": {"KHR_materials_unlit": {}},
                         "pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1]}})
            meshes.append({"name": p.name, "primitives": [{"attributes": att, "mode": 0, "material": len(mats) - 1}]})
            nodes.append({"name": p.name, "mesh": len(meshes) - 1})
            continue
        pos, nrm, col, tri = p.arrays()
        att = {"POSITION": acc(pos, 5126, "VEC3", min=pos.min(0).tolist(), max=pos.max(0).tolist()),
               "NORMAL": acc(nrm, 5126, "VEC3")}
        pbr = {"baseColorFactor": [1, 1, 1, 1], "metallicFactor": 0, "roughnessFactor": 1}
        mat = {"name": p.name, "pbrMetallicRoughness": pbr}
        if p.tex is not None:
            att["TEXCOORD_0"] = acc(np.concatenate(p.uv).astype(np.float32), 5126, "VEC2")
            buf = io.BytesIO()
            p.tex.save(buf, "JPEG", quality=88)
            imgs.append({"bufferView": view(buf.getvalue()), "mimeType": "image/jpeg"})
            texs.append({"source": len(imgs) - 1, "sampler": 0})
            pbr["baseColorTexture"] = {"index": len(texs) - 1}
            mat["extensions"] = {"KHR_materials_unlit": {}}     # the photo already has its light and shade
        else:
            att["COLOR_0"] = acc(((col / 255.0) ** 2.2).astype(np.float32), 5126, "VEC3")  # linear; photo is sRGB
        ind = acc(tri.reshape(-1), 5125, "SCALAR", target=34963)
        mats.append(mat)
        meshes.append({"name": p.name, "primitives": [{"attributes": att, "indices": ind, "material": len(mats) - 1}]})
        nodes.append({"name": p.name, "mesh": len(meshes) - 1})
    gltf = {"asset": {"version": "2.0", "generator": "hongo-campus-minecraft mesh.py"},
            "extensionsUsed": ["KHR_materials_unlit"],
            "scene": 0, "scenes": [{"nodes": list(range(len(nodes)))}], "nodes": nodes,
            "meshes": meshes, "materials": mats, "accessors": accs, "bufferViews": views,
            "buffers": [{"byteLength": len(bin_)}]}
    if imgs:
        gltf.update(images=imgs, textures=texs,
                    samplers=[{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}])
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    bin_.extend(b"\0" * (-len(bin_) % 4))
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(bin_)))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(bin_), 0x004E4942) + bin_)


def main():
    from PIL import Image
    ap = argparse.ArgumentParser()
    ap.add_argument("cells"); ap.add_argument("bldg"); ap.add_argument("out")
    ap.add_argument("--rows", type=int, nargs=2, required=True)
    ap.add_argument("--cols", type=int, nargs=2, required=True)
    ap.add_argument("--tree-max", type=int, default=1_000_000, help="thin the tree points to at most this many")
    a = ap.parse_args()
    d = np.load(a.cells)
    cell = float(d["cell"]) if "cell" in d else 1.0
    win = np.s_[a.rows[0]:a.rows[1], a.cols[0]:a.cols[1]]
    lab, top, ground, bid, rgb = d["label"][win], d["top"][win], d["ground"][win], d["bid"][win], d["rgb"][win]
    lab, top, _ = regularise(lab, top, ground, bid, d["half"][win] if "half" in d else None, cell)
    lab, crown, trunk, _, seg = separate(lab, top, ground, cell, with_seg=True)
    H, W = lab.shape
    x0 = d["origin"][0] + a.cols[0] * cell          # west edge of the window (EPSG:6677)
    y1 = d["origin"][1] - a.rows[0] * cell          # north edge
    cx, cy, base = x0 + W * cell / 2, y1 - H * cell / 2, float(np.nanmin(ground))
    xs = x0 + (np.arange(W) + 0.5) * cell - cx      # local east of each column centre
    ys = y1 - (np.arange(H) + 0.5) * cell - cy      # local north of each row centre
    X, Y = np.meshgrid(xs, ys)
    g = ground - base
    t = top - base

    P, C, K = points.load(a.cells, a.rows, a.cols)   # same local frame
    Wm, Hm = W * cell, H * cell
    span = (-Wm / 2, Hm / 2, Wm, Hm)
    uvmap = lambda pos: np.column_stack([(pos[:, 0] + Wm / 2) / Wm, (Hm / 2 - pos[:, 1]) / Hm])
    gimg, gcov = topdown(P, C, K == 2, span, TOP, highest=False)
    rimg, rcov = topdown(P, C, K != 2, span, TOP, highest=True)
    parts = {"ground": Part("ground", Image.fromarray(gimg), uvmap),
             "roofs": Part("roofs", Image.fromarray(rimg), uvmap),
             "walls": Part("walls", tex=True)}
    gs = ndi.gaussian_filter(g, SMOOTH / cell, mode="nearest")
    terrain(parts["ground"], gs, np.zeros((H, W, 3), np.uint8), xs, ys)

    def ground_at(p):
        c = np.clip(((p[:, 0] + cx - x0) / cell - 0.5).round().astype(int), 0, W - 1)
        r = np.clip(((y1 - (p[:, 1] + cy)) / cell - 0.5).round().astype(int), 0, H - 1)
        return gs[r, c]

    wt = WallTex(P, C)
    feats = json.load(open(a.bldg))["features"]
    frame = box(x0, y1 - H * cell, x0 + W * cell, y1)
    nb = ntri = 0
    for b in np.unique(bid[lab == BUILDING]):
        m = (lab == BUILDING) & (bid == b)
        poly = shapely.make_valid(shape(feats[b]["geometry"])).intersection(frame)
        polys = getattr(poly, "geoms", [poly])
        pts_xy = np.column_stack([X[m], Y[m]])
        wall = (np.median(rgb[m], 0) * 0.85).astype(np.uint8)
        fseg = _facets(np.where(m, t, np.nan), m)[0][m]
        for p in polys:
            if p.geom_type != "Polygon" or p.area < 4 * cell * cell:
                continue
            p = shapely.affinity.translate(p, -cx, -cy)
            for region, sel in facet_regions(p, pts_xy, fseg, t[m], cell):
                ntri += building(parts["roofs"], parts["walls"], wt, region, pts_xy[sel], t[m][sel], rgb[m][sel],
                                 ground_at, cell, wall)
        nb += 1
    ah, aw = wt.finish(parts["walls"])[:2]

    # trees: the points above the ground in the tree cells
    c = np.clip(((P[:, 0] + Wm / 2) / cell).astype(int), 0, W - 1)
    r = np.clip(((Hm / 2 - P[:, 1]) / cell).astype(int), 0, H - 1)
    tp = (lab[r, c] == TREE) & (K != 2) & (P[:, 2] > g[r, c] + 0.3)
    ti = np.flatnonzero(tp)
    if len(ti) > a.tree_max:
        ti = np.sort(np.random.default_rng(0).choice(ti, a.tree_max, replace=False))
    out = [p for p in parts.values() if p.n] + [Cloud("trees", P[ti], C[ti])]
    write_glb(a.out, out)
    tris = sum(sum(len(x) for x in p.tri) for p in out if isinstance(p, Part))
    print(f"{H}x{W} cells, {len(P):,} points: {nb} buildings ({ntri} roof triangles), "
          f"{sum(p.n for p in out if isinstance(p, Part))} vertices, {tris} triangles; "
          f"ground image {gimg.shape[1]}x{gimg.shape[0]} ({gcov:.0%} of pixels hit), roofs {rcov:.0%}; "
          f"walls atlas {aw}x{ah} ({wt.hit / max(wt.px, 1):.0%} of pixels hit); "
          f"trees {int(tp.sum()):,} points, wrote {len(ti):,} -> {a.out}")


if __name__ == "__main__":
    main()
