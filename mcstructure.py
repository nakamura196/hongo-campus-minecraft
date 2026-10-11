"""Turn a <tile>_cells.npz into Bedrock .mcstructure files packed as a behavior pack.

    python mcstructure.py CELLS.npz OUT.mcpack --rows R0 R1 --cols C0 C1 --at X Y Z [--chunk 64]

The window is cut into CHUNK x CHUNK column blocks; each becomes
structures/hongo/<name>.mcstructure inside the pack, and functions/hongo/load.mcfunction
holds one `structure load` per block. In the world (with the pack enabled):

    /function hongo/load

--at X Y Z means the same as in build.py. Empty space above the stacks is air,
so whatever was there before is cleared.

.mcstructure is little-endian NBT: size, block_indices (layer 0 = blocks,
layer 1 = waterlog, -1 = none) in x, y, z order with z fastest, and a palette.
"""
import argparse, io, json, os, struct, uuid, zipfile
import numpy as np
from build import columns, LEAVES

VERSION = 18153475            # block-state version 1.21.0.3; newer games upgrade it
BLOCKS = {                    # build.py block id -> (name, states)
    "air": ("minecraft:air", {}),
    "dirt": ("minecraft:dirt", {}),
    "grass_block": ("minecraft:grass_block", {}),
    "water": ("minecraft:water", {"liquid_depth": 0}),
    "stone": ("minecraft:stone", {}),
    "light_gray_concrete": ("minecraft:light_gray_concrete", {}),
    "cobblestone": ("minecraft:cobblestone", {}),
    LEAVES: ("minecraft:oak_leaves", {"persistent_bit": 1, "update_bit": 0}),
}

# --- minimal little-endian NBT writer -------------------------------------------------
END, BYTE, INT, STRING, LIST, COMPOUND = 0, 1, 3, 8, 9, 10


def _str(s):
    b = s.encode("utf-8")
    return struct.pack("<H", len(b)) + b


def _payload(tag, v):
    if tag == BYTE:
        return struct.pack("<b", v)
    if tag == INT:
        return struct.pack("<i", v)
    if tag == STRING:
        return _str(v)
    if tag == COMPOUND:              # v: list of (tag, name, value)
        return b"".join(bytes([t]) + _str(n) + _payload(t, x) for t, n, x in v) + bytes([END])
    if tag == LIST:                  # v: (element tag, items); int lists may be numpy arrays
        et, items = v
        if et == INT and isinstance(items, np.ndarray):
            return bytes([INT]) + struct.pack("<i", len(items)) + items.astype("<i4").tobytes()
        return bytes([et if len(items) else END]) + struct.pack("<i", len(items)) + \
            b"".join(_payload(et, x) for x in items)
    raise ValueError(tag)


def nbt(root):
    return bytes([COMPOUND]) + _str("") + _payload(COMPOUND, root)


def states(d):
    return [(BYTE, k, v) if k.endswith("_bit") else (INT, k, v) for k, v in d.items()]


def structure(stacks, y0, height):
    """One .mcstructure (bytes) for a block of columns; x = column, z = row."""
    H, W = stacks.shape
    pal = ["air"]
    vol = np.zeros((W, height, H), np.int32)            # x, y, z
    for (r, c), s in np.ndenumerate(stacks):
        for block, a, b in s:
            if block not in pal:
                pal.append(block)
            vol[c, a - y0:b - y0 + 1, r] = pal.index(block)
    blocks = lambda p: BLOCKS.get(p, (f"minecraft:{p}", {}))  # photo-colour blocks share Java ids
    palette = [[(STRING, "name", blocks(p)[0]), (COMPOUND, "states", states(blocks(p)[1])),
                (INT, "version", VERSION)] for p in pal]
    root = [
        (INT, "format_version", 1),
        (LIST, "size", (INT, np.array([W, height, H]))),
        (LIST, "structure_world_origin", (INT, np.array([0, 0, 0]))),
        (COMPOUND, "structure", [
            (LIST, "block_indices", (LIST, [(INT, vol.ravel()), (INT, np.full(vol.size, -1))])),
            (LIST, "entities", (COMPOUND, [])),
            (COMPOUND, "palette", [(COMPOUND, "default", [
                (LIST, "block_palette", (COMPOUND, palette)),
                (COMPOUND, "block_position_data", []),
            ])]),
        ]),
    ]
    return nbt(root)


def manifest(name):
    u = lambda part: str(uuid.uuid5(uuid.NAMESPACE_URL, f"hongo-campus-minecraft/{name}/{part}"))
    return {"format_version": 2,                        # uuids stay the same for the same pack name
            "header": {"name": f"Hongo {name}", "description": "Hongo campus from Tokyo LiDAR + PLATEAU",
                       "uuid": u("header"), "version": [1, 0, 0], "min_engine_version": [1, 21, 0]},
            "modules": [{"type": "data", "uuid": u("data"), "version": [1, 0, 0]}]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cells"); ap.add_argument("out")
    ap.add_argument("--rows", type=int, nargs=2, required=True)
    ap.add_argument("--cols", type=int, nargs=2, required=True)
    ap.add_argument("--at", type=int, nargs=3, required=True, metavar=("X", "Y", "Z"))
    ap.add_argument("--chunk", type=int, default=64)
    a = ap.parse_args()
    d = np.load(a.cells)
    win = np.s_[a.rows[0]:a.rows[1], a.cols[0]:a.cols[1]]
    x0, base_y, z0 = a.at
    stacks = columns(d["label"][win], d["top"][win], d["ground"][win], base_y)
    top = max(seg[2] for s in stacks.flat for seg in s)
    height = top - base_y + 1
    name = os.path.splitext(os.path.basename(a.out))[0]
    loads = []
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        H, W = stacks.shape
        for r in range(0, H, a.chunk):
            for c in range(0, W, a.chunk):
                part = f"r{r:04d}c{c:04d}"
                z.writestr(f"structures/hongo/{part}.mcstructure",
                           structure(stacks[r:r + a.chunk, c:c + a.chunk], base_y, height))
                loads.append(f"structure load hongo:{part} {x0 + c} {base_y} {z0 + r}")
        z.writestr("functions/hongo/load.mcfunction", "\n".join(loads) + "\n")
        z.writestr("manifest.json", json.dumps(manifest(name), indent=2))
    print(f"{H}x{W} cells, y {base_y}..{top} -> {len(loads)} structures in {a.out}")


if __name__ == "__main__":
    main()
