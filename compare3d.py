"""Write a IIIF Presentation 4.0 (draft) manifest that puts several models of one place in a Choice,
so the viewer can switch between them (the LiDAR points and the PLATEAU buildings of Ueno).

    python3 compare3d.py out/iiif/ueno.json --bbox -5134 -32100 -4878 -31812 \
        --base https://nakamura196.github.io/hongo-campus-iiif/ \
        --model plateau-ueno.glb "PLATEAU LOD2（写真つき）" "PLATEAU LOD2 (textured)" \
        --model ueno-points-light.glb "点群（軽量）" "Points (light)" ...

The first model is shown first. No "progressive" behavior: these are alternatives, not levels.
The rectangle (EPSG:6677) gives navPlace and four ground control points at its corners for
the georeferencing annotation, in the axes of points.py (x east, y up, z south, origin at the centre).
"""
import argparse, json
import pyproj

from iiif3d import CTX, lang


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("X0", "Y0", "X1", "Y1"))
    ap.add_argument("--base", required=True)
    ap.add_argument("--model", nargs=3, action="append", required=True, metavar=("FILE", "JA", "EN"))
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
                       "target": {"type": "SpecificResource", "source": [{"id": scene, "type": "Scene"}]}}]}]}],
    }
    json.dump(manifest, open(a.out, "w"), ensure_ascii=False, indent=1)
    print(f"{a.out}: {len(a.model)} models")


if __name__ == "__main__":
    main()
