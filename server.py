"""A local Java Edition server for trying builds, driven from the command line.

    python server.py setup [DATAPACK.zip]   # download server.jar, write a superflat config, add the pack
    python server.py start                  # run the server (foreground; stop with `python server.py cmd stop`)
    python server.py cmd "COMMAND" ...      # send commands over RCON and print the replies
    python server.py shot OUT.png           # capture the game window, even behind other windows
    python server.py map                    # 3D web map of the world (BlueMap) at http://127.0.0.1:8100

Everything lives in server/ (not in Git). The server listens on 127.0.0.1 only;
connect from the game with Multiplayer -> Direct Connection -> localhost.
The RCON password is generated once, kept only in server/server.properties.
"""
import hashlib, json, os, secrets, shutil, socket, struct, subprocess, sys, urllib.request

VERSION = "26.3"
MC = os.path.expanduser("~/Library/Application Support/minecraft")
JAVA = f"{MC}/runtime/java-runtime-epsilon/mac-os-arm64/java-runtime-epsilon/jre.bundle/Contents/Home/bin/java"
DIR = "server"
PROPS = {
    "server-ip": "127.0.0.1",
    "level-name": "hongo",
    "level-type": "minecraft\\:flat",
    "gamemode": "creative",
    "difficulty": "peaceful",
    "spawn-protection": "0",
    "generate-structures": "false",
    "enable-rcon": "true",
    "rcon.port": "25575",
    "view-distance": "16",
}


def props():
    path = f"{DIR}/server.properties"
    return dict(l.rstrip("\n").split("=", 1) for l in open(path) if "=" in l and not l.startswith("#"))


def setup(pack=None):
    os.makedirs(DIR, exist_ok=True)
    jar = f"{DIR}/server.jar"
    if not os.path.exists(jar):
        d = json.load(open(f"{MC}/versions/{VERSION}/{VERSION}.json"))["downloads"]["server"]
        print("downloading server.jar", VERSION)
        data = urllib.request.urlopen(d["url"]).read()
        assert hashlib.sha1(data).hexdigest() == d["sha1"], "server.jar checksum mismatch"
        open(jar, "wb").write(data)
    path = f"{DIR}/server.properties"
    old = props() if os.path.exists(path) else {}
    p = {**old, **PROPS, "rcon.password": old.get("rcon.password") or secrets.token_urlsafe(24)}
    open(path, "w").write("".join(f"{k}={v}\n" for k, v in p.items()))
    open(f"{DIR}/eula.txt", "w").write("eula=true\n")   # Minecraft EULA, accepted by the player
    if pack:
        os.makedirs(f"{DIR}/hongo/datapacks", exist_ok=True)
        shutil.copy(pack, f"{DIR}/hongo/datapacks/{os.path.basename(pack)}")
    print("ready:", os.path.abspath(DIR))


# BlueMap CLI: renders the world files into a 3D web map and serves it. Pinned by hash;
# release notes list the Minecraft versions it reads (5.28: 1.13.2 - 26.3).
BLUEMAP = ("5.28", "c6868465f8f972a64a3f2acc32112cab8560339045747f81359bdea6959f7ac2")
MAP_CONF = {      # file -> {key: value}, applied over BlueMap's generated defaults
    "core.conf": {"accept-download": "true",   # client jar from Mojang, for block models and textures
                  "metrics": "false"},
    "webserver.conf": {"ip": '"127.0.0.1"',
                       # merged over the default block (1-day cache): re-rendered tiles show on a plain reload
                       '"additional-headers"': '{ "Cache-Control": "no-cache" }'},
    "maps/overworld.conf": {"world": '"../hongo"', "name": '"Hongo"',
                            "start-pos": "{ x: 128, z: 144 }",
                            "remove-caves-below-y": "-1000"},   # the build sits at y -60..: keep it all
}


def bluemap():
    """Render server/hongo into a web map, then keep serving it and follow changes in the world."""
    d = f"{DIR}/bluemap"
    os.makedirs(d, exist_ok=True)
    jar = f"{d}/bluemap-cli.jar"
    if not os.path.exists(jar):
        v, sha = BLUEMAP
        url = f"https://github.com/BlueMap-Minecraft/BlueMap/releases/download/v{v}/bluemap-{v}-cli.jar"
        print("downloading BlueMap", v)
        data = urllib.request.urlopen(url).read()
        assert hashlib.sha256(data).hexdigest() == sha, "bluemap-cli.jar checksum mismatch"
        open(jar, "wb").write(data)
    if os.path.exists("out/grass_pack.zip"):                   # grass slabs and stairs (grasspack.py)
        os.makedirs(f"{d}/config/packs", exist_ok=True)
        shutil.copy("out/grass_pack.zip", f"{d}/config/packs/grass_pack.zip")
    os.chdir(d)
    if not os.path.exists("config/core.conf"):
        subprocess.run([JAVA, "-jar", "bluemap-cli.jar", "-c", "config", "-v", VERSION], check=True,
                       capture_output=True)                    # writes the default configs
    for m in ("nether", "end"):                                # superflat: only the overworld matters
        if os.path.exists(f"config/maps/{m}.conf"):
            os.remove(f"config/maps/{m}.conf")
    for f, kv in MAP_CONF.items():
        lines = [l for l in open(f"config/{f}") if l.split(":")[0].strip() not in kv]
        open(f"config/{f}", "w").write("".join(lines) + "".join(f"{k}: {v}\n" for k, v in kv.items()))
    print("map: http://127.0.0.1:8100  (first render takes a few minutes)")
    os.execv(JAVA, [JAVA, "-Xmx2G", "-jar", "bluemap-cli.jar", "-c", "config", "-v", VERSION, "-r", "-u", "-w"])


def start():
    os.chdir(DIR)
    os.execv(JAVA, [JAVA, "-Xmx4G", "-jar", "server.jar", "nogui"])


def cmd(commands):
    p = props()
    s = socket.create_connection(("127.0.0.1", int(p["rcon.port"])), timeout=60)

    def send(i, kind, body):
        b = body.encode() + b"\0\0"
        s.sendall(struct.pack("<iii", len(b) + 8, i, kind) + b)

    def recv():
        n = struct.unpack("<i", s.recv(4, socket.MSG_WAITALL))[0]
        b = s.recv(n, socket.MSG_WAITALL)
        return struct.unpack("<i", b[:4])[0], b[8:-2].decode("utf-8", "replace")

    send(1, 3, p["rcon.password"])
    if recv()[0] == -1:
        sys.exit("RCON login failed")
    for i, c in enumerate(commands, 2):
        send(i, 2, c)
        print(f"> {c}\n{recv()[1]}")
    s.close()


# Lists on-screen windows; the game window's title starts with "Minecraft <version>".
WINDOWS = """import CoreGraphics
for w in CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as! [[String: Any]] {
    print(w[kCGWindowNumber as String]!, w[kCGWindowName as String] ?? "")
}"""


def shot(out):
    """Capture the game window by its number (works while it is hidden behind other windows).
    If the game pauses when it loses focus, the menu covers the view: press F3+P in the game."""
    lines = subprocess.run(["swift", "-"], input=WINDOWS, capture_output=True, text=True).stdout.splitlines()
    ids = [l.split(" ", 1)[0] for l in lines if l.partition(" ")[2].startswith(f"Minecraft {VERSION}")]
    if not ids:
        sys.exit("game window not found (is Minecraft running?)")
    subprocess.run(["screencapture", "-x", "-o", "-l", ids[0], out], check=True)
    print(out)


if __name__ == "__main__":
    a = sys.argv[1:]
    {"setup": lambda: setup(*a[1:2]), "start": start, "cmd": lambda: cmd(a[1:]),
     "shot": lambda: shot(a[1]), "map": bluemap}[a[0]]()
