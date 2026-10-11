"""Turn the PLATEAU LOD2/LOD3 buildings of a window into a textured .glb, in the frame of points.py.

    python3 plateau_glb.py data/plateau/13106_53394652_bldg.gml out/ueno/plateau.glb \
        --bbox -5134 -32100 -4878 -31812 --base 1.99 \
        --images https://assets.cms.plateau.reearth.io/.../udx/bldg/

A building is kept when the middle of its vertices lies in the rectangle (EPSG:6677, m). Its
surfaces come from the highest LOD it has (lod3MultiSurface, else lod2MultiSurface). Polygons
with a ParameterizedTexture get its photo and texture coordinates (one material per photo,
unlit, since the photos already carry light and shadow); the rest are plain grey and lit.
Photos are fetched from --images (the folder of the .gml on the PLATEAU CMS) into
data/plateau/<gml name>_appearance/ and embedded as they are.

--ground TILE... adds the ground as a 1 m grid from the LiDAR ground points (class 2) of those
tiles, coloured by their photo RGB (cells without ground, under buildings, take the nearest);
PLATEAU buildings carry no ground of their own.

Axes as in points.py: x east, y up, z south, metres, origin at the centre of the rectangle,
height 0 at --base (the base points.py printed). Heights are the GML's (Tokyo Bay mean sea level,
as the LiDAR).
"""
import argparse, json, os, struct, sys, urllib.request
import numpy as np, pyproj, mapbox_earcut
from lxml import etree
from scipy import ndimage

GML = "{http://www.opengis.net/gml}"
APP = "{http://www.opengis.net/citygml/appearance/2.0}"


def lname(e):
    return etree.QName(e).localname


def ring_coords(ring):
    v = np.array(ring.findtext(f"{GML}posList").split(), float).reshape(-1, 3)
    return v[:-1] if len(v) > 1 and np.allclose(v[0], v[-1]) else v


def buildings(path, bbox, to_plane):
    """Polygons of the buildings in the rectangle: list of (polygon id, [(ring id, lat/lon/h array)])."""
    x0, y0, x1, y1 = bbox
    out, n = [], 0
    for _, el in etree.iterparse(path, tag="{*}Building", huge_tree=True):
        if lname(el.getparent()) != "cityObjectMember":
            continue
        surf = {}
        for ms in el.iter("{*}lod2MultiSurface", "{*}lod3MultiSurface"):
            surf.setdefault(lname(ms)[:4], []).extend(ms.iter(f"{GML}Polygon"))
        polys = surf.get("lod3") or surf.get("lod2") or []
        if polys:
            allv = np.vstack([ring_coords(r) for p in polys for r in p.iter(f"{GML}LinearRing")])
            mx, my = to_plane.transform(*np.median(allv[:, [1, 0]], axis=0))
            if x0 <= mx < x1 and y0 < my <= y1:
                n += 1
                out.append([(p.get(f"{GML}id"), [(r.get(f"{GML}id"), ring_coords(r)) for r in p.iter(f"{GML}LinearRing")])
                            for p in polys])
        el.clear()
    print(f"{n} buildings", file=sys.stderr)
    return [p for b in out for p in b]


def textures(path, wanted):
    """ring id -> (image name, uv array) for the rings we use."""
    tex = {}
    for _, el in etree.iterparse(path, tag=f"{APP}ParameterizedTexture", huge_tree=True):
        img = el.findtext(f"{APP}imageURI")
        for tc in el.iter(f"{APP}textureCoordinates"):
            rid = tc.get("ring", "").lstrip("#")
            if rid in wanted:
                uv = np.array(tc.text.split(), float).reshape(-1, 2)
                tex[rid] = (img, uv[:-1] if len(uv) > 1 and np.allclose(uv[0], uv[-1]) else uv)
        el.clear()
    return tex


def triangulate(rings):
    """Triangles (indices into the stacked ring vertices) of a planar polygon with holes."""
    P = np.vstack(rings)
    c = P - P.mean(0)
    nrm = np.sum(np.cross(c, np.roll(c, -1, 0)), 0)        # Newell's normal of the stacked rings
    drop = int(np.argmax(np.abs(nrm)))
    uv = np.delete(P, drop, 1)
    ends = np.cumsum([len(r) for r in rings]).astype(np.uint32)
    tri = np.asarray(mapbox_earcut.triangulate_float64(uv, ends), np.uint32).reshape(-1, 3)
    n0 = nrm / (np.linalg.norm(nrm) or 1)
    if len(tri):                                            # wind the triangles with the outer ring
        a, b, d = P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]]
        flip = np.cross(b - a, d - a) @ n0 < 0
        tri[flip] = tri[flip][:, [0, 2, 1]]
    return tri, n0


def ground(tiles, bbox, base):
    """The ground as a grid mesh: (positions, colours as linear RGB, triangle indices)."""
    from points import load
    P, C, K = load(None, None, None, tiles, bbox)     # local east, north, up from points.py's own base
    g = K == 2
    P, C = P[g], C[g].astype(float)
    x0, y0, x1, y1 = bbox
    W, H = int(x1 - x0), int(y1 - y0)
    c = np.clip(((P[:, 0] + W / 2)).astype(int), 0, W - 1)
    r = np.clip(((H / 2 - P[:, 1])).astype(int), 0, H - 1)
    k = r * W + c
    n = np.bincount(k, minlength=W * H)
    z = np.bincount(k, P[:, 2], W * H) / np.maximum(n, 1)
    rgb = np.column_stack([np.bincount(k, C[:, i], W * H) for i in range(3)]) / np.maximum(n, 1)[:, None]
    empty = (n == 0).reshape(H, W)
    _, (ri, ci) = ndimage.distance_transform_edt(empty, return_indices=True)
    near = (ri * W + ci).ravel()
    z, rgb = z[near], rgb[near]
    xs, ys = np.meshgrid(np.arange(W) + 0.5 - W / 2, H / 2 - np.arange(H) - 0.5)
    pos = np.column_stack([xs.ravel(), z, -ys.ravel()])
    i = np.arange(W * H).reshape(H, W)
    a, b, d, e = i[:-1, :-1].ravel(), i[:-1, 1:].ravel(), i[1:, :-1].ravel(), i[1:, 1:].ravel()
    tri = np.column_stack([a, d, b, b, d, e]).reshape(-1, 3)
    return pos, (rgb / 255) ** 2.2, tri


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gml"); ap.add_argument("out")
    ap.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("X0", "Y0", "X1", "Y1"))
    ap.add_argument("--base", type=float, required=True)
    ap.add_argument("--images", required=True, help="URL of the folder holding <name>_appearance/")
    ap.add_argument("--ground", nargs="+", metavar="TILE", help="LiDAR tiles for the ground")
    a = ap.parse_args()
    to_plane = pyproj.Transformer.from_crs(6668, 6677, always_xy=True)
    cx, cy = (a.bbox[0] + a.bbox[2]) / 2, (a.bbox[1] + a.bbox[3]) / 2

    polys = buildings(a.gml, a.bbox, to_plane)
    tex = textures(a.gml, {rid for _, rings in polys for rid, _ in rings})

    groups = {}                     # image name or None -> lists of positions, normals, uvs, indices
    for _, rings in polys:
        rings = [(rid, v) for rid, v in rings if len(v) >= 3]
        if not rings:
            continue
        xy = [np.column_stack(to_plane.transform(v[:, 1], v[:, 0])) for _, v in rings]
        loc = [np.column_stack([q[:, 0] - cx, v[:, 2] - a.base, -(q[:, 1] - cy)]) for q, (_, v) in zip(xy, rings)]
        tri, n0 = triangulate([np.column_stack([q, v[:, 2]]) for q, (_, v) in zip(xy, rings)])
        if not len(tri):
            continue
        t = tex.get(rings[0][0])
        img = t[0] if t and all(r in tex and len(tex[r][1]) == len(v) for r, v in rings) else None
        g = groups.setdefault(img, {"P": [], "N": [], "T": [], "I": [], "n": 0})
        P = np.vstack(loc)
        g["I"].append(tri + g["n"]); g["n"] += len(P)
        g["P"].append(P)
        g["N"].append(np.tile([n0[0], n0[2], -n0[1]], (len(P), 1)))
        if img:
            uv = np.vstack([tex[r][1] for r, _ in rings])
            g["T"].append(np.column_stack([uv[:, 0], 1 - uv[:, 1]]))   # glTF v runs down the image

    # fetch the photos
    folder = os.path.join(os.path.dirname(a.gml), os.path.basename(a.gml).replace("_bldg.gml", "_appearance"))
    os.makedirs(folder, exist_ok=True)
    blobs = {}
    for img in sorted(k for k in groups if k):
        f = os.path.join(folder, os.path.basename(img))
        if not os.path.exists(f):
            with urllib.request.urlopen(a.images + img) as r, open(f, "wb") as o:
                o.write(r.read())
        blobs[img] = open(f, "rb").read()

    body, views, accs, prims, mats, images = bytearray(), [], [], [], [], []

    def add(arr, target=None):
        b = arr.tobytes()
        views.append({"buffer": 0, "byteOffset": len(body), "byteLength": len(b), **({"target": target} if target else {})})
        body.extend(b + b"\0" * (-len(b) % 4))
        return len(views) - 1

    def acc(arr, kind, comp, target, minmax=False):
        a_ = {"bufferView": add(arr, target), "componentType": comp, "count": len(arr), "type": kind}
        if minmax:
            a_ |= {"min": arr.min(0).tolist(), "max": arr.max(0).tolist()}
        accs.append(a_)
        return len(accs) - 1

    grey = None
    for img, g in groups.items():
        attrs = {"POSITION": acc(np.vstack(g["P"]).astype(np.float32), "VEC3", 5126, 34962, True),
                 "NORMAL": acc(np.vstack(g["N"]).astype(np.float32), "VEC3", 5126, 34962)}
        if img:
            attrs["TEXCOORD_0"] = acc(np.vstack(g["T"]).astype(np.float32), "VEC2", 5126, 34962)
            images.append({"bufferView": add(np.frombuffer(blobs[img], np.uint8)), "mimeType": "image/jpeg"})
            mats.append({"name": os.path.basename(img), "doubleSided": True,
                         "pbrMetallicRoughness": {"baseColorTexture": {"index": len(images) - 1},
                                                  "metallicFactor": 0, "roughnessFactor": 1},
                         "extensions": {"KHR_materials_unlit": {}}})
            m = len(mats) - 1
        else:
            mats.append({"name": "untextured", "doubleSided": True,
                         "pbrMetallicRoughness": {"baseColorFactor": [0.75, 0.75, 0.73, 1], "metallicFactor": 0,
                                                  "roughnessFactor": 1}})
            m = grey = len(mats) - 1
        prims.append({"attributes": attrs, "indices": acc(np.vstack(g["I"]).ravel().astype(np.uint32), "SCALAR", 5125, 34963),
                      "material": m})

    if a.ground:
        gp, gc, gt = ground(a.ground, a.bbox, a.base)
        mats.append({"name": "ground", "pbrMetallicRoughness": {"metallicFactor": 0, "roughnessFactor": 1},
                     "extensions": {"KHR_materials_unlit": {}}})
        prims.append({"attributes": {"POSITION": acc(gp.astype(np.float32), "VEC3", 5126, 34962, True),
                                     "COLOR_0": acc(gc.astype(np.float32), "VEC3", 5126, 34962)},
                      "indices": acc(gt.ravel().astype(np.uint32), "SCALAR", 5125, 34963), "material": len(mats) - 1})
        groups["ground"] = {"I": [gt]}

    gltf = {"asset": {"version": "2.0", "generator": "hongo-campus-minecraft plateau_glb.py"},
            "extensionsUsed": ["KHR_materials_unlit"],
            "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"name": "plateau", "mesh": 0}],
            "meshes": [{"name": "plateau", "primitives": prims}],
            "materials": mats, "accessors": accs, "bufferViews": views, "buffers": [{"byteLength": len(body)}]}
    if images:
        gltf |= {"images": images, "textures": [{"source": i, "sampler": 0} for i in range(len(images))],
                 "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}]}
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    with open(a.out, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(body)))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(body), 0x004E4942) + bytes(body))
    ntri = sum(len(np.vstack(g["I"])) for g in groups.values())
    textured = sum(len(np.vstack(g["I"])) for k, g in groups.items() if k and k != "ground")
    print(f"{a.out}: {ntri:,} triangles ({textured / ntri:.0%} textured), {len(images)} photos, "
          f"{os.path.getsize(a.out) / 1e6:.1f} MB", file=sys.stderr)


if __name__ == "__main__":
    main()
