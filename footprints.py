"""Extract PLATEAU building outlines (lod0RoofEdge) as GeoJSON in EPSG:6677.

PLATEAU CityGML is in EPSG:6697 (lat, lon, height). The LiDAR tiles are in
JGD2011 plane zone IX (EPSG:6677), so we convert here once.

    python footprints.py OUT.geojson IN1.gml [IN2.gml ...] [--bbox xmin ymin xmax ymax]

Roof edge (屋根外形) is used rather than the ground footprint because the
LiDAR sees buildings from above; eaves overhang the footprint.
"""
import json, sys
import xml.etree.ElementTree as ET
from pyproj import Transformer

NS = {
    "gml": "http://www.opengis.net/gml",
    "bldg": "http://www.opengis.net/citygml/building/2.0",
}
T = Transformer.from_crs(6697, 6677, always_xy=True)


def rings(edge):
    for pl in edge.iter(f"{{{NS['gml']}}}posList"):
        v = [float(s) for s in pl.text.split()]
        lat, lon = v[0::3], v[1::3]
        xs, ys = T.transform(lon, lat)
        yield [[round(x, 3), round(y, 3)] for x, y in zip(xs, ys)]


def main():
    args = sys.argv[1:]
    bbox = None
    if "--bbox" in args:
        i = args.index("--bbox")
        bbox = [float(a) for a in args[i + 1:i + 5]]
        del args[i:i + 5]
    out, inputs = args[0], args[1:]
    feats, seen = [], set()
    for path in inputs:
        for _, el in ET.iterparse(path):
            if el.tag != f"{{{NS['bldg']}}}Building":
                continue
            gid = el.get(f"{{{NS['gml']}}}id")
            edge = el.find("bldg:lod0RoofEdge", NS)
            if edge is None:
                edge = el.find("bldg:lod0FootPrint", NS)
            h = el.findtext("bldg:measuredHeight", namespaces=NS)
            name = el.findtext("gml:name", namespaces=NS)
            if edge is not None and gid not in seen:
                polys = list(rings(edge))
                xs = [p[0] for r in polys for p in r]
                ys = [p[1] for r in polys for p in r]
                if not bbox or (max(xs) > bbox[0] and min(xs) < bbox[2] and max(ys) > bbox[1] and min(ys) < bbox[3]):
                    seen.add(gid)
                    feats.append({"type": "Feature", "properties": {
                        "id": gid, "height": float(h) if h else None, "name": name},
                        "geometry": {"type": "MultiPolygon", "coordinates": [[r] for r in polys]}})
            el.clear()
    json.dump({"type": "FeatureCollection", "crs": "EPSG:6677", "features": feats},
              open(out, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"{len(feats)} buildings -> {out}")


if __name__ == "__main__":
    main()
