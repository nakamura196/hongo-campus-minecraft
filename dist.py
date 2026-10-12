"""A clean world to hand out: the main build only, spawn in front of Yasuda Auditorium,
the grass pack bundled as resources.zip.

    python dist.py out/wide.zip out/grass_pack.zip out/hongo-campus.zip

Builds in server-dist/ (not in Git) with a second server on its own ports, so the
everyday server (server.py, server/) can keep running. The steps:
a superflat world -> place every piece of the data pack (forceload each piece first,
since nobody is standing there) -> spawn point and game rules -> stop -> remove the
data pack (it is only the placing commands, 50 MB) -> add resources.zip and README ->
zip the world folder.

The player unzips into .minecraft/saves/ and opens the world; the game offers the
bundled resource pack when the world loads.

    python dist.py out/wide.zip out/grass_pack.zip out/hongo-campus.zip --bedrock out/hongo-campus.mcworld

also converts the finished world for Bedrock Edition with Chunker (Hive Games, MIT;
the jar is fetched once into server-dist/ and checked against its SHA-256). The format
is Bedrock 1.21.130, old enough for most phones and tablets; newer games open it too.
The grass pack is Java only, so on Bedrock the slopes are plain grass blocks.
"""
import argparse, hashlib, os, re, shutil, subprocess, sys, tempfile, time, urllib.request, zipfile
import server

DIR = "server-dist"
WORLD = "hongo-campus"
PORTS = {"server-port": "25566", "rcon.port": "25576"}
SPAWN = "3502 -37 890 -90 0"         # in front of Yasuda Auditorium, facing east (toward it)
RULES = {"respawn_radius": 0, "advance_time": "false", "advance_weather": "false",
         "spawn_mobs": "false", "spawn_phantoms": "false"}
CHUNKER = ("https://github.com/HiveGamesOSS/Chunker/releases/download/1.21.0/chunker-cli-1.21.0.jar",
           "63148e587f5ee5a2ca7ebae1079b235c82ae4d1eff8f7a8a8a3398332513dda6")
BEDROCK = "BEDROCK_R21_130"

README = """Hongo Campus (University of Tokyo) in Minecraft Java Edition {version}

Open: unzip into the saves folder of the game and choose the world "{world}".
Accept the resource pack when asked: without it, the grass slopes look like blue-green planks.

Scale: 1 block = 1 m. Built automatically from aerial laser scanning and PLATEAU.

Sources:
- 東京都デジタルツイン実現プロジェクト 区部点群データ (Tokyo Metropolitan Government,
  Digital Service Bureau), CC BY 4.0, via G空間情報センター.
- 3D 都市モデル（Project PLATEAU）文京区 (国土交通省), processed for this world
  (building outlines and heights reduced to blocks). This world is not made or endorsed
  by the Ministry.

License: CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). Author: Satoru Nakamura
(The University of Tokyo). Credit this work and the sources above.
Download and updates: https://github.com/nakamura196/hongo-campus-minecraft-world

Not an official Minecraft product. Not approved by or associated with Mojang or Microsoft.
"""


def rcon(*commands):
    out = subprocess.run([sys.executable, "-c", "import server, sys; server.DIR = sys.argv[1]; "
                          "server.cmd(sys.argv[2:])", DIR, *commands],
                         capture_output=True, text=True, check=True).stdout
    return out


def wait_ready(proc, log):
    for _ in range(300):
        if proc.poll() is not None:
            sys.exit(f"server stopped early, see {log}")
        if "RCON running" in open(log).read():
            return
        time.sleep(1)
    sys.exit("server did not start in 5 minutes")


def pieces(pack):
    """(function name, x0, z0, x1, z1) for each piece, from the header line of each function."""
    z = zipfile.ZipFile(pack)
    out = []
    for n in sorted(z.namelist(), key=lambda n: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", n)]):
        m = re.fullmatch(r"data/(\w+)/function/(\w+)\.mcfunction", n)
        if not m:
            continue
        xs, zs = set(), set()
        for line in z.read(n).decode().splitlines():
            if line.startswith(("fill ", "setblock ")):
                t = line.split()
                xs.update((int(t[1]), int(t[4]) if t[0] == "fill" else int(t[1])))
                zs.update((int(t[3]), int(t[6]) if t[0] == "fill" else int(t[3])))
        out.append((f"{m[1]}:{m[2]}", min(xs), min(zs), max(xs), max(zs)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pack"); ap.add_argument("resources"); ap.add_argument("out")
    ap.add_argument("--package-only", action="store_true", help="skip placing; re-pack the world already built")
    ap.add_argument("--bedrock", metavar="OUT.mcworld", help="also write a Bedrock Edition world")
    a = ap.parse_args()
    if not a.package_only:
        build(a)
    package(a)
    if a.bedrock:
        bedrock(a.bedrock)


def build(a):
    if os.path.exists(f"{DIR}/{WORLD}"):
        sys.exit(f"{DIR}/{WORLD} exists; remove it to build again")

    server.DIR = DIR
    os.makedirs(DIR, exist_ok=True)
    if not os.path.exists(f"{DIR}/server.jar"):
        shutil.copy("server/server.jar", f"{DIR}/server.jar")
    server.PROPS.update({"level-name": WORLD, **PORTS})
    server.setup()
    os.makedirs(f"{DIR}/{WORLD}/datapacks")
    shutil.copy(a.pack, f"{DIR}/{WORLD}/datapacks/{os.path.basename(a.pack)}")

    log = os.path.abspath(f"{DIR}/run.log")
    proc = subprocess.Popen([server.JAVA, "-Xmx4G", "-jar", "server.jar", "nogui"], cwd=DIR,
                            stdout=open(log, "w"), stderr=subprocess.STDOUT)
    try:
        wait_ready(proc, log)
        for name, x0, z0, x1, z1 in pieces(a.pack):
            r = rcon(f"forceload add {x0} {z0} {x1} {z1}", f"function {name}", "forceload remove all")
            if "unction" not in r.splitlines()[3]:      # "Running function ..." / "Executed ..."
                sys.exit(f"{name}: {r}")
            print(name, x0, z0, x1, z1, flush=True)
        print(rcon(f"setworldspawn {SPAWN}", *(f"gamerule {k} {v}" for k, v in RULES.items()),
                   "time set 6000", "weather clear", "save-all flush"))
    finally:
        rcon("stop")
        proc.wait(timeout=300)


def package(a):
    world = f"{DIR}/{WORLD}"
    shutil.rmtree(f"{world}/datapacks", ignore_errors=True)
    for junk in ("session.lock", "level.dat_old", "players"):
        p = f"{world}/{junk}"
        shutil.rmtree(p) if os.path.isdir(p) else os.path.exists(p) and os.remove(p)
    shutil.copy(a.resources, f"{world}/resources.zip")
    open(f"{world}/README.txt", "w").write(README.format(version=server.VERSION, world=WORLD))

    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(world):
            for f in files:
                p = os.path.join(root, f)
                z.write(p, os.path.relpath(p, DIR))
    print(a.out, f"{os.path.getsize(a.out) / 1e6:.1f} MB")


def bedrock(out):
    url, sha = CHUNKER
    jar = f"{DIR}/{os.path.basename(url)}"
    if not os.path.exists(jar):
        os.makedirs(DIR, exist_ok=True)
        data = urllib.request.urlopen(url).read()
        if hashlib.sha256(data).hexdigest() != sha:
            sys.exit(f"{url}: SHA-256 does not match")
        open(jar, "wb").write(data)
    with tempfile.TemporaryDirectory() as tmp:
        dst = f"{tmp}/{WORLD}"
        subprocess.run([server.JAVA, "-Xmx4G", "-jar", jar, "-i", f"{DIR}/{WORLD}", "-o", dst, "-f", BEDROCK],
                       check=True, stdout=subprocess.DEVNULL)
        open(f"{dst}/levelname.txt", "w").write(WORLD)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(dst):
                for f in files:
                    p = os.path.join(root, f)
                    z.write(p, os.path.relpath(p, dst))
    print(out, f"{os.path.getsize(out) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
