"""Write a IIIF Presentation 4.0 (draft) manifest that puts several models of one place in a Choice,
so the viewer can switch between them (the LiDAR points and the PLATEAU buildings of Ueno).

    python3 compare3d.py out/iiif/ueno.json --bbox -5134 -32100 -4878 -31812 \
        --base https://nakamura196.github.io/hongo-campus-iiif/ \
        --model plateau-ueno.glb "PLATEAU LOD2（写真つき）" "PLATEAU LOD2 (textured)" \
        --model ueno-points-light.glb "点群（軽量）" "Points (light)" ... \
        --labels data/plateau/13106_53394652_bldg.gml 1.99

The first model is shown first. No "progressive" behavior: these are alternatives, not levels.
The rectangle (EPSG:6677) gives navPlace and four ground control points at its corners for
the georeferencing annotation, in the axes of points.py (x east, y up, z south, origin at the centre).

--labels GML BASE adds a commenting annotation (a pin) on the roof of each PLATEAU building in the
rectangle that has a name (gml:name), and on the tallest one, each with a camera (<pin id>/camera) to view
it from. BASE is the same --base as plateau_glb.py.
"""
import argparse, json
import numpy as np
import pyproj
from lxml import etree

from iiif3d import CTX, lang

GML = "{http://www.opengis.net/gml}"
# PLATEAU has the names in Japanese only
NAMES_EN = {"上野駅": "Ueno Station", "上野病院": "Ueno Hospital", "上野警察署上野駅前交番": "Ueno Station Police Box"}


def named_buildings(path, base, bbox, to_plane):
    """(ja name or None, x, roof y, z, measured height) of the buildings in the rectangle that have a name, plus the tallest."""
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    out = []
    for _, el in etree.iterparse(path, tag="{*}Building", huge_tree=True):
        if etree.QName(el.getparent()).localname != "cityObjectMember":
            continue
        rings = [r for ms in el.iter("{*}lod2MultiSurface", "{*}lod3MultiSurface") for r in ms.iter(f"{GML}LinearRing")]
        if rings:
            v = np.vstack([np.array(r.findtext(f"{GML}posList").split(), float).reshape(-1, 3) for r in rings])
            mx, my = to_plane.transform(*np.median(v[:, [1, 0]], axis=0))    # same test as plateau_glb.py
            if x0 <= mx < x1 and y0 < my <= y1:
                h = el.findtext("{*}measuredHeight")
                out.append((el.findtext(f"{GML}name"), mx - cx, v[:, 2].max() - base, -(my - cy), float(h) if h else None))
        el.clear()
    tallest = max(out, key=lambda b: b[2])
    return [b for b in out if b[0]] + ([] if tallest[0] else [tallest])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("X0", "Y0", "X1", "Y1"))
    ap.add_argument("--base", required=True)
    ap.add_argument("--model", nargs=3, action="append", required=True, metavar=("FILE", "JA", "EN"))
    ap.add_argument("--labels", nargs=2, metavar=("GML", "BASE"))
    a = ap.parse_args()
    x0, y0, x1, y1 = a.bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    to_lonlat = pyproj.Transformer.from_crs(6677, 4326, always_xy=True)

    def lonlat(x, z):
        lon, lat = to_lonlat.transform(cx + x, cy - z)
        return [round(lon, 7), round(lat, 7)]

    mid = a.base + a.out.split("/")[-1]
    scene = mid + "#scene"
    hw, hh = (x1 - x0) / 2, (y1 - y0) / 2
    corners = [(-hw, -hh, "北西", "NW"), (hw, -hh, "北東", "NE"), (hw, hh, "南東", "SE"), (-hw, hh, "南西", "SW")]

    def target(x, y, z):
        return {"type": "SpecificResource", "source": [{"id": scene, "type": "Scene"}],
                "selector": [{"type": "PointSelector", "x": round(x, 2), "y": round(y, 2), "z": round(z, 2)}]}

    painting = [{
        "id": mid + "#model", "type": "Annotation", "motivation": ["painting"],
        "body": {"type": "Choice", "label": lang("表示する模型", "Model"),
                 "items": [{"id": a.base + f, "type": "Model", "format": "model/gltf-binary", "label": lang(ja, en)}
                           for f, ja, en in a.model]},
        "target": target(0, 0, 0)}, {
        "id": mid + "#overview", "type": "Annotation", "motivation": ["painting"],
        "body": {"type": "PerspectiveCamera", "label": lang("全体", "Overview"), "fieldOfView": 50,
                 "lookAt": {"type": "PointSelector", "x": 0, "y": 0, "z": 0}},
        "target": target(0, 220, 300)}]
    gcps = [{"type": "Feature", "id": f"{mid}#gcp-{en}",
             "properties": {"title": f"範囲の{ja}の角", "resourceCoords": [round(x, 2), 0, round(z, 2)]},
             "geometry": {"type": "Point", "coordinates": lonlat(x, z)}} for x, z, ja, en in corners]

    pins = []
    if a.labels:
        for i, (name, x, y, z, h) in enumerate(named_buildings(a.labels[0], float(a.labels[1]), a.bbox,
                                                               pyproj.Transformer.from_crs(6668, 6677, always_xy=True))):
            ja = name or "この範囲でいちばん高い建物"
            en = NAMES_EN.get(name, name) if name else "The tallest building in this area"
            pins.append({"id": f"{mid}#building-{i + 1}", "type": "Annotation", "motivation": ["commenting"],
                         "label": lang(ja, en),
                         "body": {"type": "TextualBody", "format": "text/plain",
                                  "value": (f"高さ {h:g} m / Height {h:g} m " if h else "") + "(PLATEAU)"},
                         "target": target(x, y + 2, z)})    # a little above the roof
            # the view when the pin is clicked: from the south-east and above, so the streets around stay in view
            pins.append({"id": f"{mid}#building-{i + 1}/camera", "type": "Annotation", "motivation": ["painting"],
                         "body": {"type": "PerspectiveCamera", "fieldOfView": 50,
                                  "lookAt": {"type": "PointSelector", "x": round(x, 2), "y": round(y, 2), "z": round(z, 2)}},
                         "target": target(x + 60, y + 100, z + 150)})

    manifest = {
        "@context": ["http://iiif.io/api/extension/georef/1/context.json",
                     "http://iiif.io/api/extension/navplace/context.json", CTX],    # presentation last
        "id": mid, "type": "Manifest",
        "label": lang("上野駅周辺の点群と PLATEAU 建物モデルの比較", "Ueno station area: LiDAR points and PLATEAU buildings compared"),
        "summary": lang("同じ約 260 × 290 m の範囲を、航空レーザー測量の点群（2023年3〜4月計測）と、"
                        "PLATEAU の建物モデル（LOD2・空中写真のテクスチャつき）で示す。左下の切り替えで見比べられる。",
                        "The same area of about 260 x 290 m as airborne LiDAR points (surveyed March-April 2023) "
                        "and as PLATEAU building models (LOD2, textured from aerial photos). Switch between them."),
        "requiredStatement": {
            "label": lang("出典", "Attribution"),
            "value": lang("点群: 東京都「東京都デジタルツイン実現プロジェクト 区部点群データ」(CC BY 4.0) を加工。"
                          "建物: 国土交通省 Project PLATEAU「3D都市モデル（台東区）2025年度」を加工。",
                          "Points: Tokyo Metropolitan Government, Tokyo Digital Twin Project, ward-area point cloud (CC BY 4.0), processed. "
                          "Buildings: MLIT Project PLATEAU, 3D city model of Taito-ku (FY2025), processed.")},
        "navPlace": {"id": mid + "#place", "type": "FeatureCollection", "features": [{
            "id": mid + "#place/1", "type": "Feature", "properties": {"label": lang("模型の範囲", "Extent of the model")},
            "geometry": {"type": "Polygon", "coordinates": [[lonlat(x, z) for x, z, *_ in corners + corners[:1]]]}}]},
        "items": [{"id": scene, "type": "Scene", "label": lang("上野駅周辺", "Around Ueno station"),
                   "spatialScale": {"type": "UnitValue", "quantityValue": 1.0, "unit": "m"},
                   "items": [{"id": mid + "#painting", "type": "AnnotationPage", "items": painting}],
                   "annotations": [{"id": mid + "#georef", "type": "AnnotationPage", "items": [{
                       "id": mid + "#georef/1", "type": "Annotation", "motivation": ["georeferencing"],
                       "body": {"type": "FeatureCollection",
                                "transformation": {"type": "polynomial", "options": {"order": 1}}, "features": gcps},
                       "target": {"type": "SpecificResource", "source": [{"id": scene, "type": "Scene"}]}}]}]
                                  + ([{"id": mid + "#buildings", "type": "AnnotationPage", "items": pins}] if pins else [])}],
    }
    json.dump(manifest, open(a.out, "w"), ensure_ascii=False, indent=1)
    print(f"{a.out}: {len(a.model)} models, {len(pins) // 2} pins")


if __name__ == "__main__":
    main()
