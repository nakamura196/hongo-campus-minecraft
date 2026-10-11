"""Resource pack: warped slabs and stairs that look like grass, for slopes on grass.

    python grasspack.py out/grass_pack.zip

Minecraft has no grass slab or grass stairs, so smooth.py cannot round grassy ground
(most of it under trees) to the half block. The build places warped_slab / warped_stairs
there instead (warped wood is used nowhere else), and this pack redraws them as grass.

Grass is grey in the game files and tinted per biome by the game's code; that tint does not
reach other blocks, so the texture here is green already, in the plains colour (the flat
world is all plains). Side faces are grass too (the top texture), not the grass block's
side: on half-block steps the dirt below the green rim showed as brown flecks.

Nothing is copied from the game, so the pack can be shared: the texture is drawn here
(seeded noise, brightness spread like the game's grass, about +-12 %), and the models only
name the game's own slab and stair shapes as their parent.
"""
import argparse, io, json, os, zipfile
import numpy as np
from PIL import Image

RESOURCE_FORMAT = 97                 # Java 26.3
DONOR = "warped"
PLAINS = (145, 189, 89)              # plains grass colour (#91BD59)
SPREAD = 0.12                        # brightness spread of the game's grass top (std / mean)
SEED = 6677
SHADE = 0.578                        # mean grey of the game's grass top (147 / 255): the tint multiplies it


def grass_top(size=16):
    rng = np.random.default_rng(SEED)
    fine = rng.normal(0, 1, (size, size))
    coarse = rng.normal(0, 1, (size // 2, size // 2)).repeat(2, 0).repeat(2, 1)
    n = fine + 0.35 * coarse
    k = SHADE * (1 + SPREAD * (n - n.mean()) / n.std())
    rgb = np.clip(np.array(PLAINS)[None, None] * k[..., None], 0, 255).astype(np.uint8)
    b = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(b, "PNG")
    return b.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    a = ap.parse_args()
    files = {"assets/hongo/textures/block/grass_top.png": grass_top()}
    tex = {"top": "hongo:block/grass_top", "side": "hongo:block/grass_top",
           "bottom": "minecraft:block/dirt"}
    for model, parent in (("slab", "slab"), ("stairs", "stairs"),
                          ("stairs_inner", "inner_stairs"), ("stairs_outer", "outer_stairs")):
        m = {"parent": f"minecraft:block/{parent}", "textures": tex}
        files[f"assets/minecraft/models/block/{DONOR}_{model}.json"] = json.dumps(m, indent=1).encode()
    meta = {"pack": {"description": "Hongo campus: grass slabs and stairs (warped wood redrawn)",
                     "min_format": RESOURCE_FORMAT, "max_format": RESOURCE_FORMAT,
                     "pack_format": RESOURCE_FORMAT}}
    files["pack.mcmeta"] = json.dumps(meta, indent=2).encode()
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        for p, data in sorted(files.items()):
            z.writestr(p, data)
    print(f"{a.out}: plains grass colour {PLAINS}, {len(files)} files")


if __name__ == "__main__":
    main()
