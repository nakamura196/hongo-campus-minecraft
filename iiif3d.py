"""Write a IIIF Presentation 4.0 (draft) manifest for the point cloud, for 3d.ldas.jp.

    python iiif3d.py CELLS.npz out/iiif/hongo.json --rows 200 488 --cols 220 476 \
        --base http://127.0.0.1:8124/ --models hongo-light.glb hongo-mid.glb hongo-full.glb

The models (lod.py, then gltfpack -c) are painted as one Choice with behavior "progressive",
lightest first: 3d.ldas.jp shows the first at once and swaps in the last when it has loaded;
other viewers show the first. Each named building of OpenStreetMap (data/osm/buildings.json)
becomes a commenting annotation at a point above its roof, with a hidden camera tied to it
by `scope`. The text comes from data/osm/descriptions.json (name -> ja/en/sources); a building
without one gets its name only.

Georeferencing: the same points also go into one `georeferencing` annotation (IIIF Georeference
Extension, with resourceCoords in 3D as 3d.ldas.jp/georef reads it) as ground control points,
lon/lat paired with scene x, y, z; and the window outline goes into the Manifest's navPlace.

Axes as in points.py: x east, y up, z south, metres, origin at the window centre, height 0 at
the lowest ground of the window.
"""
import argparse, json
import numpy as np, pyproj
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union, polygonize

from points import window

CTX = "http://iiif.io/api/presentation/4/context.json"
SKIP = {"ローソン"}   # shops inside a building, not buildings of the campus


def outlines(osm):
    """name -> (tags, shapely polygon in lon/lat) for each named building."""
    out = {}
    for e in osm["elements"]:
        t = e.get("tags", {})
        name = t.get("name", "").split(";")[-1].strip()
        if not name or name in SKIP:
            continue
        if e["type"] == "way":
            poly = Polygon([(p["lon"], p["lat"]) for p in e["geometry"]])
        else:
            rings = [[(p["lon"], p["lat"]) for p in m["geometry"]] for m in e["members"] if m.get("role") == "outer"]
            lines = [LineString(r) for r in rings if len(r) > 1]
            poly = unary_union(list(polygonize(unary_union(lines))))
        out[name] = (dict(t, _id=f"{e['type'][0]}{e['id']}"), poly)
    return out


def lang(ja, en):
    m = {}
    if ja:
        m["ja"] = [ja]
    if en:
        m["en"] = [en]
    return m


def html(text, sources):
    s = f"<p>{text}</p>" if text else ""
    if sources:
        links = "、".join(f'<a href="{x["url"]}" target="_blank" rel="noopener">{x["title"]}</a>' for x in sources)
        s += f"<p>出典: {links}</p>"
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cells"); ap.add_argument("out")
    ap.add_argument("--rows", type=int, nargs=2, required=True)
    ap.add_argument("--cols", type=int, nargs=2, required=True)
    ap.add_argument("--base", required=True, help="URL the manifest and models are served from (ends with /)")
    ap.add_argument("--models", nargs="+", required=True, help="model files, lightest first")
    ap.add_argument("--osm", default="data/osm/buildings.json")
    ap.add_argument("--text", default="data/osm/descriptions.json")
    a = ap.parse_args()

    d, cell, x0, y1, W, H, cx, cy, base = window(a.cells, a.rows, a.cols)
    top = d["top"][a.rows[0]:a.rows[1], a.cols[0]:a.cols[1]] - base
    to_plane = pyproj.Transformer.from_crs(4326, 6677, always_xy=True)
    to_lonlat = pyproj.Transformer.from_crs(6677, 4326, always_xy=True)

    def lonlat(x, z):
        lon, lat = to_lonlat.transform(cx + x, cy - z)
        return [round(lon, 7), round(lat, 7)]
    texts = {}
    try:
        texts = {t["name"]: t for t in json.load(open(a.text))}
    except FileNotFoundError:
        pass

    mid = a.base + a.out.split("/")[-1]
    scene = mid + "#scene"
    sizes = ("軽量", "中", "高精細")[-len(a.models):] if len(a.models) <= 3 else [str(i) for i in range(len(a.models))]
    sizes_en = ("light", "medium", "full")[-len(a.models):] if len(a.models) <= 3 else sizes

    def target(x, y, z):
        return {"type": "SpecificResource", "source": [{"id": scene, "type": "Scene"}],
                "selector": [{"type": "PointSelector", "x": round(x, 2), "y": round(y, 2), "z": round(z, 2)}]}

    painting = [{
        "id": mid + "#model", "type": "Annotation", "motivation": ["painting"],
        "body": {"type": "Choice", "behavior": ["progressive"],
                 "label": lang("点群の密度", "Point density"),
                 "items": [{"id": a.base + m, "type": "Model", "format": "model/gltf-binary",
                            "label": lang(sizes[i], sizes_en[i])} for i, m in enumerate(a.models)]},
        "target": target(0, 0, 0)}, {
        "id": mid + "#overview", "type": "Annotation", "motivation": ["painting"],
        "body": {"type": "PerspectiveCamera", "label": lang("全体", "Overview"), "fieldOfView": 50,
                 "lookAt": {"type": "PointSelector", "x": 0, "y": 0, "z": 0}},
        "target": target(0, 220, 300)}]

    notes, cams, gcps = [], [], []
    for name, (tags, poly) in sorted(outlines(json.load(open(a.osm))).items(), key=lambda kv: kv[0]):
        if poly.is_empty:
            continue
        xy = np.array([to_plane.transform(lon, lat) for lon, lat in poly.exterior.coords]) if isinstance(poly, Polygon) \
            else None
        geom = Polygon(xy) if xy is not None else MultiPolygon(
            [Polygon([to_plane.transform(*c) for c in p.exterior.coords]) for p in poly.geoms])
        # the roof: the highest tenth of the cells inside the outline
        c0, r0 = (geom.bounds[0] - x0) / cell, (y1 - geom.bounds[3]) / cell
        c1, r1 = (geom.bounds[2] - x0) / cell, (y1 - geom.bounds[1]) / cell
        hs = [top[r, c] for r in range(max(int(r0), 0), min(int(r1) + 1, H))
              for c in range(max(int(c0), 0), min(int(c1) + 1, W))
              if geom.contains(Point(x0 + (c + 0.5) * cell, y1 - (r + 0.5) * cell)) and np.isfinite(top[r, c])]
        inside = geom.intersection(Polygon([(x0, y1), (x0 + W * cell, y1), (x0 + W * cell, y1 - H * cell), (x0, y1 - H * cell)]))
        if not hs or inside.area < 0.5 * geom.area:     # mostly outside the window
            continue
        p = inside.representative_point() if not geom.contains(geom.centroid) else geom.centroid
        x, z, y = p.x - cx, -(p.y - cy), float(np.percentile(hs, 90)) + 3
        t = texts.get(name, {})
        aid = f"{mid}#osm-{tags['_id']}"
        cam = aid + "/camera"
        # look at the building from the south-east, above its roof
        cams.append({"id": cam, "type": "Annotation", "motivation": ["painting"], "behavior": ["hidden"],
                     "body": {"type": "PerspectiveCamera", "fieldOfView": 50,
                              "lookAt": {"type": "PointSelector", "x": round(x, 2), "y": round(y - 3, 2), "z": round(z, 2)}},
                     "target": target(x + 45, y + 55, z + 75)})
        bodies = [{"type": "TextualBody", "format": "text/html", "language": "ja",
                   "value": html(t.get("ja"), t.get("sources"))}]
        if t.get("en"):
            bodies.append({"type": "TextualBody", "format": "text/html", "language": "en",
                           "value": html(t.get("en"), t.get("sources")).replace("出典: ", "Source: ").replace("、", ", ")})
        notes.append({"id": aid, "type": "Annotation", "motivation": ["commenting"],
                      "label": lang(name, t.get("name_en") or tags.get("name:en")),
                      "body": bodies, "scope": [cam], "target": target(x, y, z)})
        gcps.append({"type": "Feature", "id": aid + "/gcp",
                     "properties": {"title": name, "resourceCoords": [round(x, 2), round(y, 2), round(z, 2)]},
                     "geometry": {"type": "Point", "coordinates": lonlat(x, z)}})

    manifest = {
        "@context": ["http://iiif.io/api/extension/georef/1/context.json",
                     "http://iiif.io/api/extension/navplace/context.json", CTX],    # presentation last
        "id": mid, "type": "Manifest",
        "label": lang("東京大学 本郷キャンパス（安田講堂周辺）の点群", "Point cloud of the University of Tokyo Hongo campus (around Yasuda Auditorium)"),
        "summary": lang("航空レーザー測量の点群（2023年3〜4月計測）を、約 260 × 290 m の範囲で示す。建物の説明は注釈から。",
                        "Airborne LiDAR points (surveyed March-April 2023) over about 260 x 290 m. Buildings are described in the annotations."),
        "requiredStatement": {
            "label": lang("出典", "Attribution"),
            "value": lang("点群: 東京都「東京都デジタルツイン実現プロジェクト 区部点群データ」(CC BY 4.0) を加工。建物の名前と位置: © OpenStreetMap contributors (ODbL)",
                          "Points: Tokyo Metropolitan Government, Tokyo Digital Twin Project, ward-area point cloud (CC BY 4.0), processed. Building names and outlines: © OpenStreetMap contributors (ODbL)")},
        "navPlace": {"id": mid + "#place", "type": "FeatureCollection", "features": [{
            "id": mid + "#place/1", "type": "Feature", "properties": {"label": lang("模型の範囲", "Extent of the model")},
            "geometry": {"type": "Polygon", "coordinates": [[lonlat(sx * W * cell / 2, sz * H * cell / 2)
                                                             for sx, sz in ((-1, -1), (1, -1), (1, 1), (-1, 1), (-1, -1))]]}}]},
        "items": [{"id": scene, "type": "Scene", "label": lang("本郷キャンパス", "Hongo campus"),
                   "spatialScale": {"type": "UnitValue", "quantityValue": 1.0, "unit": "m"},
                   "items": [{"id": mid + "#painting", "type": "AnnotationPage", "items": painting}],
                   "annotations": [{"id": mid + "#notes", "type": "AnnotationPage", "items": notes},
                                   {"id": mid + "#cameras", "type": "AnnotationPage", "items": cams},
                                   {"id": mid + "#georef", "type": "AnnotationPage", "items": [{
                                       "id": mid + "#georef/1", "type": "Annotation", "motivation": ["georeferencing"],
                                       "body": {"type": "FeatureCollection",
                                                "transformation": {"type": "polynomial", "options": {"order": 1}}, "features": gcps},
                                       "target": {"type": "SpecificResource", "source": [{"id": scene, "type": "Scene"}]}}]}]}],
    }
    json.dump(manifest, open(a.out, "w"), ensure_ascii=False, indent=1)
    print(f"{a.out}: {len(notes)} buildings, {sum(1 for n in notes if n['label'] and texts.get(n['label']['ja'][0]))} with text")


if __name__ == "__main__":
    main()
