"""Download openly licensed photos of one building from Wikimedia Commons, with their attribution.

    python3 photos.py data/photos/yasuda --category "Yasuda Auditorium" --search "Yasuda Auditorium" --search "安田講堂"

Candidates are the files of the category plus the full-text search hits. Only CC0, public domain,
CC BY and CC BY-SA files are kept (the licence is read from the file page's metadata, not guessed).
Each photo is fetched as a thumbnail at most --width pixels wide (enough for structure from motion,
and kinder to the servers than the originals). The attribution of every kept file goes to
<out>/sources.json; files that were skipped are listed there too, with the reason.
"""
import argparse, json, os, re, sys, time, urllib.parse, urllib.request

API = "https://commons.wikimedia.org/w/api.php"
UA = "hongo-campus-research/0.1 (https://github.com/nakamura196/hongo-campus-minecraft-world; nakamura.satoru@mail.u-tokyo.ac.jp)"
OPEN = re.compile(r"^(CC0|Public domain|PD|CC BY(-SA)? [0-9.]+( [a-z]+)?)", re.I)


def get(params):
    q = urllib.parse.urlencode({**params, "format": "json"})
    req = urllib.request.Request(f"{API}?{q}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def candidates(categories, searches):
    titles = []
    for c in categories:
        p = {"action": "query", "list": "categorymembers", "cmtitle": f"Category:{c}", "cmtype": "file", "cmlimit": 500}
        titles += [m["title"] for m in get(p)["query"]["categorymembers"]]
    for s in searches:
        p = {"action": "query", "list": "search", "srsearch": s, "srnamespace": 6, "srlimit": 500}
        titles += [m["title"] for m in get(p)["query"]["search"]]
    return list(dict.fromkeys(titles))


def info(titles, width):
    out = {}
    for i in range(0, len(titles), 40):
        p = {"action": "query", "titles": "|".join(titles[i:i + 40]), "prop": "imageinfo",
             "iiprop": "url|size|mime|extmetadata", "iiurlwidth": width}
        for page in get(p)["query"]["pages"].values():
            if "imageinfo" in page:
                out[page["title"]] = page["imageinfo"][0]
    return out


def text(meta, key):
    v = meta.get(key, {}).get("value", "")
    return re.sub(r"<[^>]+>", "", str(v)).strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--category", action="append", default=[])
    ap.add_argument("--search", action="append", default=[])
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--min-side", type=int, default=800, help="skip photos smaller than this")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    titles = candidates(a.category, a.search)
    meta = info(titles, a.width)
    kept, skipped = [], []
    for t in titles:
        ii = meta.get(t)
        if ii is None:
            skipped.append({"title": t, "reason": "no image info"}); continue
        m = ii.get("extmetadata", {})
        lic = text(m, "LicenseShortName")
        if ii["mime"] not in ("image/jpeg", "image/png"):
            skipped.append({"title": t, "reason": f"type {ii['mime']}"}); continue
        if not OPEN.match(lic) or "NC" in lic or "ND" in lic:
            skipped.append({"title": t, "reason": f"licence {lic!r}"}); continue
        if min(ii["width"], ii["height"]) < a.min_side:
            skipped.append({"title": t, "reason": f"small {ii['width']}x{ii['height']}"}); continue
        name = re.sub(r"[^\w.-]+", "_", t.removeprefix("File:"))
        if ii["mime"] == "image/png":
            name = os.path.splitext(name)[0] + ".png"
        path = os.path.join(a.out, name)
        if not os.path.exists(path):
            req = urllib.request.Request(ii["thumburl"], headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
                f.write(r.read())
            time.sleep(0.5)
        kept.append({"file": name, "title": t, "page": ii["descriptionurl"], "licence": lic,
                     "licence_url": text(m, "LicenseUrl"), "artist": text(m, "Artist"),
                     "credit": text(m, "Credit"), "date": text(m, "DateTimeOriginal"),
                     "original_size": [ii["width"], ii["height"]]})
    with open(os.path.join(a.out, "sources.json"), "w") as f:
        json.dump({"kept": kept, "skipped": skipped}, f, ensure_ascii=False, indent=1)
    print(f"{len(titles)} candidates, {len(kept)} kept, {len(skipped)} skipped -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
