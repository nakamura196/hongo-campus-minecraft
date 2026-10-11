"""Make lighter copies of a point-cloud .glb (points.py) for the web: quantised, optionally thinned.

    python lod.py out/vr/points.glb out/iiif/points-full.glb
    python lod.py out/vr/points.glb out/iiif/points-light.glb --voxel 0.5

Positions become 16-bit integers in centimetres (KHR_mesh_quantization, with the scale on the
node), colours 8-bit RGBA: 12 bytes a point instead of 20. --voxel keeps one point per cube of
that size (the first one found), which thins dense roofs and ground but keeps sparse walls.
"""
import argparse, json, struct
import numpy as np

STEP = 0.01   # m per integer step of the stored positions


def read(path):
    b = open(path, "rb").read()
    n = struct.unpack("<I", b[12:16])[0]
    j = json.loads(b[20:20 + n])
    binary = b[20 + n + 8:]
    prim = j["meshes"][0]["primitives"][0]
    out = []
    for name in ("POSITION", "COLOR_0"):
        a = j["accessors"][prim["attributes"][name]]
        v = j["bufferViews"][a["bufferView"]]
        dt = {5126: np.float32, 5123: np.uint16, 5121: np.uint8}[a["componentType"]]
        k = {"VEC3": 3, "VEC4": 4}[a["type"]]
        x = np.frombuffer(binary, dt, a["count"] * k, v.get("byteOffset", 0) + a.get("byteOffset", 0))
        out.append(x.reshape(-1, k))
    P, C = out
    if C.dtype == np.uint16:
        C = (C.astype(np.uint32) * 255 // 65535).astype(np.uint8)
    elif C.dtype != np.uint8:
        C = (np.clip(C, 0, 1) * 255).round().astype(np.uint8)
    if C.shape[1] == 3:
        C = np.hstack([C, np.full((len(C), 1), 255, np.uint8)])
    return P.astype(np.float64), C


def thin(P, voxel):
    """Index of one point per voxel."""
    key = np.floor((P - P.min(0)) / voxel).astype(np.int64)
    _, keep = np.unique(key, axis=0, return_index=True)
    return np.sort(keep)


def morton(Q):
    """Order that keeps nearby points next to each other (Z-order on 1 m cells), so that the
    differences between neighbours stay small and EXT_meshopt_compression packs them well."""
    c = ((Q - Q.min(0)) // 100).astype(np.uint64)
    key = np.zeros(len(Q), np.uint64)
    for bit in range(12):
        for axis in range(3):
            key |= ((c[:, axis] >> np.uint64(bit)) & np.uint64(1)) << np.uint64(3 * bit + axis)
    return np.lexsort((Q[:, 1], key))


def write(path, P, C):
    Q = np.round(P / STEP).astype(np.int64)
    assert np.abs(Q).max() < 32768, "positions out of 16-bit range"
    o = morton(Q)
    Q, C = Q[o], C[o]
    pos = np.zeros((len(Q), 4), np.int16)           # padded to 8 bytes: vertex strides are multiples of 4
    pos[:, :3] = Q
    pb, cb = pos.tobytes(), np.ascontiguousarray(C).tobytes()
    j = {
        "asset": {"version": "2.0", "generator": "hongo-campus-minecraft lod.py"},
        "extensionsUsed": ["KHR_mesh_quantization"], "extensionsRequired": ["KHR_mesh_quantization"],
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "points", "mesh": 0, "scale": [STEP] * 3}],
        "meshes": [{"name": "points", "primitives": [{"attributes": {"POSITION": 0, "COLOR_0": 1}, "mode": 0}]}],
        "accessors": [
            {"bufferView": 0, "componentType": 5122, "count": len(Q), "type": "VEC3",
             "min": Q.min(0).tolist(), "max": Q.max(0).tolist()},
            {"bufferView": 1, "componentType": 5121, "normalized": True, "count": len(Q), "type": "VEC4"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(pb), "byteStride": 8, "target": 34962},
            {"buffer": 0, "byteOffset": len(pb), "byteLength": len(cb), "byteStride": 4, "target": 34962},
        ],
        "buffers": [{"byteLength": len(pb) + len(cb)}],
    }
    js = json.dumps(j, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    body = pb + cb
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(body)))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(body), 0x004E4942) + body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("out")
    ap.add_argument("--voxel", type=float, help="keep one point per cube of this size (m)")
    a = ap.parse_args()
    P, C = read(a.src)
    if a.voxel:
        k = thin(P, a.voxel)
        P, C = P[k], C[k]
    write(a.out, P, C)
    print(f"{a.out}: {len(P):,} points")


if __name__ == "__main__":
    main()
