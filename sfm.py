"""Structure from motion on the downloaded photos (COLMAP through pycolmap, CPU only).

    .venv/bin/python sfm.py data/photos/yasuda out/yasuda_photo/sfm

Every photo gets its own camera (they come from different people and lenses). Matching is
exhaustive (a few dozen photos). Photos that do not belong to the building (interiors, other
buildings) simply fail to register; nothing is picked by hand. The largest model is written to
<out>/model (COLMAP binary) and <out>/model.ply (sparse points), and a summary is printed.
The model is in COLMAP's own arbitrary frame; align.py brings it to EPSG:6677.
"""
import argparse, os, shutil, sys
import pycolmap


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images")
    ap.add_argument("out")
    ap.add_argument("--max-features", type=int, default=16384)
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    db = os.path.join(a.out, "database.db")
    if os.path.exists(db):
        os.remove(db)
    ext = pycolmap.FeatureExtractionOptions()
    ext.sift.max_num_features = a.max_features
    ext.sift.estimate_affine_shape = True
    ext.sift.domain_size_pooling = True
    pycolmap.extract_features(db, a.images, camera_mode=pycolmap.CameraMode.PER_IMAGE,
                              camera_model="SIMPLE_RADIAL", extraction_options=ext,
                              device=pycolmap.Device.cpu)
    match = pycolmap.FeatureMatchingOptions()
    match.guided_matching = True
    pycolmap.match_exhaustive(db, matching_options=match, device=pycolmap.Device.cpu)

    sparse = os.path.join(a.out, "sparse")
    shutil.rmtree(sparse, ignore_errors=True)
    os.makedirs(sparse)
    maps = pycolmap.incremental_mapping(db, a.images, sparse)
    if not maps:
        sys.exit("no model")
    best = max(maps.values(), key=lambda r: r.num_reg_images())
    model = os.path.join(a.out, "model")
    os.makedirs(model, exist_ok=True)
    best.write(model)
    best.export_PLY(os.path.join(a.out, "model.ply"))
    print(best.summary())
    for k, r in maps.items():
        print(f"model {k}: {r.num_reg_images()} images, {r.num_points3D()} points")
    names = sorted(img.name for img in best.images.values())
    print("registered:", *names, sep="\n  ")


if __name__ == "__main__":
    main()
