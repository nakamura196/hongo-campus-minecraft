"""Download the inputs for one LiDAR tile into data/ (skips files already there).

    python fetch.py [TILE]          # default 09LD0863

LiDAR: Tokyo 23-ku point cloud 2024 (G空間情報センター tokyopc-23ku-2024), one
.las per 1/500 sheet. PLATEAU: LOD1 buildings for the 3rd-order meshes the tile
spans, found through the PLATEAU VIEW data catalog API.
"""
import io, json, os, sys, urllib.request, zipfile

LIDAR = "https://gic-tokyo.s3.ap-northeast-1.amazonaws.com/2024/dig/lp/{tile}.zip"
CATALOG = "https://api.plateauview.mlit.go.jp/datacatalog/citygml/m:{mesh}"
MESHES = {"09LD0863": ["53394650", "53394651"]}   # add tiles here as the build grows


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.read()


def bldg_urls(obj):
    if isinstance(obj, dict):
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            yield from bldg_urls(v)
    elif isinstance(obj, str) and "/udx/bldg/" in obj and obj.endswith(".gml"):
        yield obj


def main():
    tile = sys.argv[1] if len(sys.argv) > 1 else "09LD0863"
    os.makedirs("data/lidar", exist_ok=True)
    os.makedirs("data/plateau", exist_ok=True)
    las = f"data/lidar/{tile}.las"
    if not os.path.exists(las):
        print(f"downloading {tile}.zip (~150 MB)")
        with zipfile.ZipFile(io.BytesIO(get(LIDAR.format(tile=tile)))) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".las"))
            with open(las, "wb") as f:
                f.write(z.read(name))
    for mesh in MESHES[tile]:
        for url in sorted(set(bldg_urls(json.loads(get(CATALOG.format(mesh=mesh)))))):
            ward = url.split("/")[-4][:5]                 # 13105 = 文京区, 13106 = 台東区
            out = f"data/plateau/{ward}_{mesh}_bldg.gml"
            if not os.path.exists(out):
                print("downloading", out)
                with open(out, "wb") as f:
                    f.write(get(url))
    print("done")


if __name__ == "__main__":
    main()
