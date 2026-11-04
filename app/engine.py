#!/usr/bin/env python3
"""GBRecomp Tools engine — UI-free core.

Converts Game Boy / Game Boy Color ROMs to native PC executables with
gbrecompiled (mstan's fork of gb-recompiled): probe (GB header) ->
generate (gbrecomp) -> scaffold (extras.c identity hooks + box art) ->
build (Linux / Windows) -> verify -> package. Driven by the CLI for
now (GUI is a later milestone):

    engine.py toolbox
    engine.py probe --rom game.gb
    engine.py run --job job.json

Job JSON keys:
    rom          path to the .gb / .gbc file
    title        display title (default: ROM file stem)
    output_dir   where packages + the project folder land
    targets      ["linux"], ["windows"] or both (default both)
    jobs         parallel build jobs (default 2)
    force        build even if the probe flags needs-review
    regen        delete and regenerate the gbrecomp project
    ra           include RetroAchievements (default true): the
                 bundled module (bundle/ra/) is copied into the
                 project, the generated CMake/main are patched to
                 compile it and call its game_post_frame hook, and
                 ra_toml (below) ships beside the exe. If the bundle
                 is missing the job logs a loud warning and the
                 package ships WITHOUT RetroAchievements.
    ra_popups    show achievement pop-ups / login toast (default
                 true; false compiles RA_DISABLE_POPUPS)
    ra_toml      path to a filled retroachievements.toml to ship
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
FRAMEWORK_CACHE = APP_DIR / "framework-cache"
ART_CACHE = APP_DIR / "art-cache"
RA_BUNDLE = APP_DIR / "bundle" / "ra"
# Windows cross-build support files (SDL2 / zlib / Mesa for MinGW) are
# owned by the app: fetched + built into APP_TOOLS_DIR on first use by
# ensure_win_tools(). LEGACY_TOOLS_DIR is the old developer-VM layout,
# still honored when it holds a complete set (see tools_dir()).
APP_TOOLS_DIR = APP_DIR / "tools"
LEGACY_TOOLS_DIR = Path.home() / "workspace" / "tools"

GBRECOMP_GIT = "https://github.com/mstan/gbrecompiled.git"
RECOMP_UI_GIT = "https://github.com/RetroPortingToolKit/recomp-ui.git"

# libretro-thumbnails systems, GitHub mirror first (the canonical host
# has been unreachable from some networks for whole evenings), canonical
# second — same dual-host policy as the PSX app's v1.0.15 fetcher.
BOXART_SYSTEMS = {
    "gb": [
        "https://raw.githubusercontent.com/libretro-thumbnails/Nintendo_-_Game_Boy/master/Named_Boxarts/",
        "https://thumbnails.libretro.com/Nintendo%20-%20Game%20Boy/Named_Boxarts/",
    ],
    "gbc": [
        "https://raw.githubusercontent.com/libretro-thumbnails/Nintendo_-_Game_Boy_Color/master/Named_Boxarts/",
        "https://thumbnails.libretro.com/Nintendo%20-%20Game%20Boy%20Color/Named_Boxarts/",
    ],
}


def log_line(log, msg):
    if log:
        log(msg)
    else:
        print(msg, flush=True)


# --------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------

CART_TYPES = {
    0x00: ("ROM ONLY", False), 0x01: ("MBC1", False),
    0x02: ("MBC1+RAM", False), 0x03: ("MBC1+RAM+BATTERY", True),
    0x05: ("MBC2", False), 0x06: ("MBC2+BATTERY", True),
    0x08: ("ROM+RAM", False), 0x09: ("ROM+RAM+BATTERY", True),
    0x0B: ("MMM01", False), 0x0C: ("MMM01+RAM", False),
    0x0D: ("MMM01+RAM+BATTERY", True),
    0x0F: ("MBC3+TIMER+BATTERY", True),
    0x10: ("MBC3+TIMER+RAM+BATTERY", True), 0x11: ("MBC3", False),
    0x12: ("MBC3+RAM", False), 0x13: ("MBC3+RAM+BATTERY", True),
    0x19: ("MBC5", False), 0x1A: ("MBC5+RAM", False),
    0x1B: ("MBC5+RAM+BATTERY", True), 0x1C: ("MBC5+RUMBLE", False),
    0x1D: ("MBC5+RUMBLE+RAM", False), 0x1E: ("MBC5+RUMBLE+RAM+BATTERY", True),
    0x20: ("MBC6", False), 0x22: ("MBC7+SENSOR+RUMBLE+RAM+BATTERY", True),
    0xFC: ("POCKET CAMERA", False), 0xFD: ("BANDAI TAMA5", False),
    0xFE: ("HuC3", False), 0xFF: ("HuC1+RAM+BATTERY", True),
}

RAM_SIZES = {0: 0, 1: 2, 2: 8, 3: 32, 4: 128, 5: 64}


def _rom_size_bytes(code):
    if code == 0x52:
        return int(1.1 * 1024 * 1024)
    if code == 0x53:
        return int(1.2 * 1024 * 1024)
    if code == 0x54:
        return int(1.5 * 1024 * 1024)
    if 0 <= code <= 8:
        return 32 * 1024 << code
    return 0


def probe_rom(rom_path):
    """Parse a Game Boy header (0x100-0x14F). Instant; no framework."""
    p = Path(rom_path)
    data = p.read_bytes()
    if len(data) < 0x150:
        raise ValueError("not a Game Boy ROM (smaller than the header)")
    title_raw = data[0x134:0x143].split(b"\x00")[0]
    header_title = title_raw.decode("ascii", errors="replace").strip()
    cgb_flag = data[0x143]
    sgb = data[0x146] == 0x03
    cgb = cgb_flag in (0x80, 0xC0) or p.suffix.lower() == ".gbc"
    cart = data[0x147]
    cart_name, battery = CART_TYPES.get(cart, (f"UNKNOWN 0x{cart:02X}", False))
    # Header checksum: x = 0; for 0x134..0x14C: x = x - byte - 1.
    x = 0
    for i in range(0x134, 0x14D):
        x = (x - data[i] - 1) & 0xFF
    stem = p.stem
    region = ""
    low = stem.lower()
    for tag, reg in (("(japan)", "Japan"), ("(usa)", "USA"),
                     ("(europe)", "Europe"), ("(world)", "World"),
                     ("(jue)", "World")):
        if tag in low:
            region = reg
            break
    probe = {
        "rom": str(p),
        "title": stem,
        "header_title": header_title,
        "cgb_flag": cgb_flag,
        "sgb": sgb,
        "cgb": cgb,
        "cgb_only": cgb_flag == 0xC0,
        "cart_type": cart,
        "mbc": cart_name,
        "battery": battery,
        "rom_size_code": data[0x148],
        "rom_size_kb": _rom_size_bytes(data[0x148]) // 1024,
        "ram_size_kb": RAM_SIZES.get(data[0x149], 0),
        "header_checksum_ok": x == data[0x14D],
        "region": region,
        "size": len(data),
        "crc32": "%08x" % (zlib.crc32(data) & 0xFFFFFFFF),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    probe["needs_review"] = _needs_seed_review(probe)
    return probe


def _needs_seed_review(probe):
    """Hand-seeding flags go here. Some titles need manual entry points
    (gbrecomp --config with [entry_points]) that plain auto-analysis
    misses; when such a title is identified it gets flagged here so the
    queue marks it needs-review instead of shipping a broken build.
    Tetris-class ROMs (plain auto-analysis proven) flag nothing."""
    return False


# --------------------------------------------------------------------------
# Cover art (libretro-thumbnails, both hosts, retries — same policy as
# the PSX app's v1.0.15 fetcher, plus No-Intro name variants: Duane's set
# uses GoodGBX names like "Tetris (JUE) (V1.1) [!]" whose box art is
# filed as "Tetris (World) (Rev 1)")
# --------------------------------------------------------------------------

def _fetch_bytes_retry(url, tries=3, timeout=20):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "gb-recomp-tools"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise  # a missing name is definitive; retrying can't help
            last = e
            time.sleep(1.5 * (i + 1))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def _valid_cover(data):
    if not data or len(data) < 8000:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        import struct
        if len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return w >= 200 and h >= 200
        return True
    return data[:3] == b"\xff\xd8\xff"


def _cover_name_variants(title):
    variants = [title]
    no_brackets = re.sub(r"\s*\[[^\]]*\]", "", title).strip()
    if no_brackets != title:
        variants.append(no_brackets)
    # Region-group + version normalization toward No-Intro names.
    norm = no_brackets
    norm = re.sub(r"\((JUE|JU|UE|U|J)\)", "(World)", norm)
    norm = re.sub(r"\(V(\d+)\.(\d+)\)", r"(Rev \2)", norm)
    if norm != no_brackets:
        variants.append(norm)
    core = title.split(" (")[0].strip()
    if core and core not in variants:
        variants.append(core)
    seen, out = set(), []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _enhanced_variants(bases, sgb, dual_mode):
    """No-Intro files enhancement tags AFTER the region group:
    "Tetris DX (World) (SGB Enhanced) (GB Compatible)". The plain
    variants never carry those tags, which is why Tetris DX's box
    art 404'd on every host even though the GBC set has it. The tags
    are cart facts (SGB flag 0x146, CGB flag 0x80 = dual-mode), so
    the probe supplies them. Most specific suffix first."""
    suffixes = []
    if sgb and dual_mode:
        suffixes.append(" (SGB Enhanced) (GB Compatible)")
    if sgb:
        suffixes.append(" (SGB Enhanced)")
    if dual_mode:
        suffixes.append(" (GB Compatible)")
    out = []
    for base in bases:
        cands = [base]
        if "(" not in base:
            cands.append(base + " (World)")
        for cand in cands:
            for suf in suffixes:
                out.append(cand + suf)
    return out


def fetch_cover(title, cgb=False, log=None, sgb=False, cgb_flag=None):
    """Box art for a ROM title. Returns the cached PNG path or None.
    Dual-system games try both the GB and GBC sets."""
    ART_CACHE.mkdir(parents=True, exist_ok=True)
    dest = ART_CACHE / (title + ".png")
    if dest.exists() and _valid_cover(dest.read_bytes()):
        return dest
    names = _cover_name_variants(title)
    dual_mode = cgb_flag == 0x80 if cgb_flag is not None else bool(cgb)
    extra = _enhanced_variants(names, sgb, dual_mode)
    seen = set(names)
    names += [n for n in extra if n not in seen]
    systems = ["gbc", "gb"] if cgb else ["gb", "gbc"]
    for system in systems:
        for name in names:
            url_name = urllib.parse.quote(name + ".png")
            for host in BOXART_SYSTEMS[system]:
                try:
                    data = _fetch_bytes_retry(host + url_name)
                except Exception as e:  # noqa: BLE001
                    log_line(log, f"cover: {host.split('/')[2]} failed ({e})")
                    continue
                if _valid_cover(data):
                    dest.write_bytes(data)
                    log_line(log, f"cover: fetched {name}.png [{system}] "
                                  f"({len(data)} bytes)")
                    return dest
                log_line(log, f"cover: {host.split('/')[2]} returned an "
                              f"invalid image for {name}.png")
    log_line(log, f"cover: no box art found for {title}")
    return None


def _png_to_tga(png_path, tga_path):
    """Convert the fetched box art PNG to the TGA the generated
    project's launcher staging expects (recomp/launcher/boxart.tga)."""
    try:
        from PIL import Image
        with Image.open(png_path) as im:
            im.convert("RGB").save(tga_path)
        return True
    except ImportError:
        pass
    for cmd in (["convert", str(png_path), str(tga_path)],
                ["gm", "convert", str(png_path), str(tga_path)]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, capture_output=True, timeout=60, check=True)
                return tga_path.exists()
            except Exception:  # noqa: BLE001
                continue
    return False


# --------------------------------------------------------------------------
# Toolbox
# --------------------------------------------------------------------------

APT_PACKAGES = {
    "cmake": "cmake",
    "ninja": "ninja-build",
    "gcc": "build-essential",
    "g++": "build-essential",
    "x86_64-w64-mingw32-gcc": "gcc-mingw-w64-x86-64",
    "x86_64-w64-mingw32-g++": "g++-mingw-w64-x86-64",
    "git": "git",
}


def toolbox():
    checks = []
    for tool in ["cmake", "ninja", "gcc", "g++", "git",
                 "x86_64-w64-mingw32-gcc", "x86_64-w64-mingw32-g++"]:
        checks.append({"tool": tool, "found": shutil.which(tool) is not None,
                       "apt": APT_PACKAGES[tool]})
    try:
        out = subprocess.run(["pkg-config", "--modversion", "sdl2"],
                             capture_output=True, text=True, timeout=10)
        sdl2 = out.returncode == 0
    except Exception:  # noqa: BLE001
        sdl2 = False
    checks.append({"tool": "SDL2 dev (pkg-config sdl2)", "found": sdl2,
                   "apt": "libsdl2-dev"})
    checks.append({"tool": "curl (support-file downloads)",
                   "found": shutil.which("curl") is not None,
                   "apt": "curl"})
    checks.append({"tool": "OpenSSL dev (RA HTTPS)",
                   "found": Path("/usr/include/openssl/ssl.h").exists(),
                   "apt": "libssl-dev"})
    try:
        import PIL  # noqa: F401
        pil = True
    except ImportError:
        pil = False
    checks.append({"tool": "Pillow (box-art TGA conversion)", "found": pil,
                   "apt": "python3-pil"})
    # Windows cross-build support files: fetched/built automatically
    # into the app's tools/ folder on the first Windows build (see
    # ensure_win_tools). Check the exact files the build reads — a
    # folder existing is not enough (2026-11-02 field failure: the
    # SDL2 folder was present, its CMake config was not).
    t = tools_dir()
    for label, path in (
            ("SDL2 MinGW devel (auto-fetched)",
             t / "SDL2-2.30.0" / "x86_64-w64-mingw32" / "lib" / "cmake"
             / "SDL2" / "sdl2-config.cmake"),
            ("zlib MinGW (auto-fetched)", t / "zlib-mingw" / "lib" / "libz.a"),
            ("Mesa EGL/GLES headers (auto-fetched)",
             t / "mesa-devel" / "include" / "EGL" / "egl.h"),
            ("Mesa runtime DLLs (auto-fetched)",
             t / "mesa-runtime" / "x64" / "libgallium_wgl.dll")):
        checks.append({"tool": label, "found": Path(path).exists(), "apt": None})
    py_ok = sys.version_info >= (3, 11)
    checks.append({"tool": f"Python >= 3.11 (have {sys.version.split()[0]})",
                   "found": py_ok, "apt": "python3"})
    missing = sorted({c["apt"] for c in checks if not c["found"] and c["apt"]})
    return {"checks": checks, "missing_apt": missing,
            "ready": not missing}


# --------------------------------------------------------------------------
# Frameworks
# --------------------------------------------------------------------------

def _gb_dir_candidates():
    cands = []
    if os.environ.get("GBRECOMP_DIR"):
        cands.append(Path(os.environ["GBRECOMP_DIR"]))
    cands.append(FRAMEWORK_CACHE / "gbrecompiled")
    cands.append(Path.home() / "workspace" / "gb-tool-work" / "framework-probe")
    return cands


def _ui_dir_candidates():
    return [
        FRAMEWORK_CACHE / "recomp-ui",
        Path.home() / "workspace" / "nes-recomp-tools-gui" / "framework-cache" / "recomp-ui",
        Path.home() / "workspace" / "recomp-tools-gui" / "framework-cache" / "recomp-ui",
    ]


def ensure_frameworks(log):
    """Locate (or fetch + build) the gbrecompiled framework and the
    recomp-ui checkout. The recompiler binary must exist — generated
    projects compile the boot code with the host gbrecomp, and every
    build here uses the HLE boot implementation so no Nintendo
    firmware is ever needed or shipped."""
    gb_dir = None
    for cand in _gb_dir_candidates():
        if (cand / "build" / "bin" / "gbrecomp").exists():
            gb_dir = cand
            break
    if gb_dir is None:
        dest = FRAMEWORK_CACHE / "gbrecompiled"
        if not (dest / "recompiler").exists():
            log_line(log, "fetching gbrecompiled framework…")
            _git_clone(GBRECOMP_GIT, dest, recursive=True, log=log)
        log_line(log, "building the gbrecomp recompiler (one-time, HLE boot)…")
        _run(["cmake", "-S", str(dest), "-B", str(dest / "build"),
              "-G", "Ninja", "-DGBRT_BOOT_IMPLEMENTATION=HLE"], log, timeout=1200)
        _run(["cmake", "--build", str(dest / "build"), "-j", "2"], log,
             timeout=3600)
        gb_dir = dest
    ui_dir = None
    for cand in _ui_dir_candidates():
        if (cand / "recomp_ui.cmake").exists():
            ui_dir = cand
            break
    if ui_dir is None:
        ui_dir = FRAMEWORK_CACHE / "recomp-ui"
        log_line(log, "fetching recomp-ui…")
        _git_clone(RECOMP_UI_GIT, ui_dir, recursive=False, log=log)
    log_line(log, f"framework: {gb_dir}")
    log_line(log, f"recomp-ui: {ui_dir}")
    return gb_dir, gb_dir / "build" / "bin" / "gbrecomp", ui_dir


def _git_clone(url, dest, recursive, log):
    if dest.exists():
        shutil.rmtree(dest)
    cmd = ["git", "clone", "--depth", "1"]
    if recursive:
        cmd.append("--recursive")
    cmd += [url, str(dest)]
    _run(cmd, log, timeout=1200)


def _run(cmd, log, cwd=None, timeout=None):
    log_line(log, "$ " + " ".join(str(c) for c in cmd))
    proc = subprocess.Popen([str(c) for c in cmd], cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    try:
        for line in proc.stdout:
            log_line(log, line.rstrip())
    finally:
        proc.wait(timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(str(c) for c in cmd)}")
    return proc.returncode


# --------------------------------------------------------------------------
# Windows cross-build support files (SDL2 / zlib / Mesa for MinGW)
#
# Field failure (Duane, 2026-11-02): every Windows build died at CMake
# configure on his machine — the engine pointed CMAKE_PREFIX_PATH et al.
# at ~/workspace/tools, a folder that only ever existed, hand-assembled,
# on the developer VM; his copy had the SDL2 folder but not the CMake
# package config inside it. The app now owns these files: they live in
# the app's own tools/ folder and are fetched/built automatically on
# the first Windows build, the same way the frameworks are.
# --------------------------------------------------------------------------

SDL2_MINGW_URL = ("https://github.com/libsdl-org/SDL/releases/download/"
                  "release-2.30.0/SDL2-devel-2.30.0-mingw.tar.gz")
ZLIB_SRC_URL = ("https://github.com/madler/zlib/releases/download/"
                "v1.3.1/zlib-1.3.1.tar.gz")
SEVENZIP_URL = ("https://github.com/ip7z/7zip/releases/download/26.05/"
                "7z2605-linux-x64.tar.xz")
MESA_DEVEL_URL = ("https://github.com/pal1000/mesa-dist-win/releases/"
                  "download/26.2.5/mesa3d-26.2.5-devel-mingw.7z")
MESA_RELEASE_URL = ("https://github.com/pal1000/mesa-dist-win/releases/"
                    "download/26.2.5/mesa3d-26.2.5-release-mingw.7z")


def _win_tools_ok(base):
    """True when every file the Windows build + package steps read
    from a tools folder is actually present. Directory existence is
    NOT enough (see the field failure above)."""
    if not base:
        return False
    sdl2 = base / "SDL2-2.30.0" / "x86_64-w64-mingw32"
    need = [
        sdl2 / "lib" / "cmake" / "SDL2" / "sdl2-config.cmake",
        sdl2 / "bin" / "SDL2.dll",
        base / "zlib-mingw" / "include" / "zlib.h",
        base / "zlib-mingw" / "lib" / "libz.a",
        base / "mesa-devel" / "include" / "EGL" / "egl.h",
        base / "mesa-devel" / "lib" / "x64" / "libEGL.dll.a",
        base / "mesa-runtime" / "x64" / "libEGL.dll",
        base / "mesa-runtime" / "x64" / "libGLESv2.dll",
        base / "mesa-runtime" / "x64" / "libgallium_wgl.dll",
    ]
    return all(p.exists() for p in need)


def tools_dir():
    """Where the Windows cross-build support files live. GB_TOOLS_DIR
    wins; then the app's own tools/ folder; then the legacy
    ~/workspace/tools (developer VM layout) when it is complete."""
    env = os.environ.get("GB_TOOLS_DIR")
    if env:
        return Path(env)
    if _win_tools_ok(APP_TOOLS_DIR):
        return APP_TOOLS_DIR
    if _win_tools_ok(LEGACY_TOOLS_DIR):
        return LEGACY_TOOLS_DIR
    return APP_TOOLS_DIR


def _download(url, dest, log):
    """Download a (possibly large) support file. curl first — it rides
    out the flaky TLS routes that make bare urllib fail with
    WRONG_VERSION_NUMBER / handshake errors on some networks (seen on
    the developer VM, 2026-11-02, while curl fetched the same GitHub
    release fine); urllib with retries is the fallback."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    name = url.rsplit('/', 1)[-1]
    log_line(log, f"downloading {name}…")
    # Download to a .part name and rename on success, so an
    # interrupted run never leaves a partial archive that later
    # runs would mistake for a complete one.
    part = dest.with_name(dest.name + ".part")
    if shutil.which("curl"):
        try:
            _run(["curl", "-fsSL", "--retry", "3", "--retry-delay", "2",
                  "-o", str(part), url], log, timeout=1200)
            part.replace(dest)
            log_line(log, f"downloaded {name} "
                          f"({dest.stat().st_size} bytes)")
            return dest
        except Exception as e:  # noqa: BLE001
            log_line(log, f"curl download failed ({e}); "
                          "retrying directly…")
    data = _fetch_bytes_retry(url, tries=3, timeout=180)
    part.write_bytes(data)
    part.replace(dest)
    log_line(log, f"downloaded {name} ({len(data)} bytes)")
    return dest


def _tar_extract(archive, dest):
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tf:
        try:
            tf.extractall(dest, filter="data")
        except TypeError:  # older Python without the filter argument
            tf.extractall(dest)


def _sevenzip(log, tools):
    """A BCJ2-capable 7z extractor, fetched once into the tools
    folder — py7zr cannot read the BCJ2 streams in Mesa's archives."""
    cand = tools / "7z" / "7zz"
    if cand.exists():
        return cand
    arc = tools / "_dl" / "7z2605-linux-x64.tar.xz"
    if not arc.exists():
        _download(SEVENZIP_URL, arc, log)
    _tar_extract(arc, tools / "7z")
    if not cand.exists():
        raise RuntimeError("7zz extraction did not produce 7z/7zz")
    cand.chmod(0o755)
    return cand


def _extract_7z(sevenzz, archive, dest, log):
    dest.mkdir(parents=True, exist_ok=True)
    _run([str(sevenzz), "x", str(archive), "-o" + str(dest), "-y"],
         log, timeout=1200)


def _build_zlib_mingw(tools, log):
    """zlib has no official MinGW binary package the build can use —
    build the static lib from source with the cross compiler (the
    same recipe proven by hand on the developer VM)."""
    if shutil.which("x86_64-w64-mingw32-gcc") is None:
        raise RuntimeError("MinGW cross compiler not installed — use "
                           "the toolbox Install button first")
    arc = tools / "_dl" / "zlib-1.3.1.tar.gz"
    if not arc.exists():
        _download(ZLIB_SRC_URL, arc, log)
    src = tools / "_dl" / "zlib-1.3.1"
    if src.exists():
        shutil.rmtree(src)
    _tar_extract(arc, tools / "_dl")
    out = tools / "zlib-mingw"
    for sub in ("include", "lib", "bin"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    mk = ["make", "-f", "win32/Makefile.gcc",
          "CC=x86_64-w64-mingw32-gcc", "AR=x86_64-w64-mingw32-ar",
          "RC=x86_64-w64-mingw32-windres"]
    _run(mk, log, cwd=str(src), timeout=600)
    _run(mk + ["install",
               "INCLUDE_PATH=" + str(out / "include"),
               "LIBRARY_PATH=" + str(out / "lib"),
               "BINARY_PATH=" + str(out / "bin")],
         log, cwd=str(src), timeout=300)


def ensure_win_tools(log):
    """Fetch/build the Windows cross-build support files into the
    app's tools folder on first use and return the tools dir. Raises
    a plain RuntimeError when a piece cannot be prepared — run_job
    fails only the Windows target then, never the whole job."""
    if os.environ.get("GB_TOOLS_DIR"):
        base = Path(os.environ["GB_TOOLS_DIR"])
        if not _win_tools_ok(base):
            raise RuntimeError(
                f"GB_TOOLS_DIR={base} does not hold a complete Windows "
                "tools set (SDL2 sdl2-config.cmake, "
                "zlib-mingw/lib/libz.a, Mesa headers + runtime DLLs) — "
                "fix that folder or unset GB_TOOLS_DIR and let the app "
                "fetch its own")
        return base
    if _win_tools_ok(APP_TOOLS_DIR):
        return APP_TOOLS_DIR
    if _win_tools_ok(LEGACY_TOOLS_DIR):
        return LEGACY_TOOLS_DIR
    base = APP_TOOLS_DIR
    base.mkdir(parents=True, exist_ok=True)
    sdl2 = base / "SDL2-2.30.0" / "x86_64-w64-mingw32"
    if not (sdl2 / "lib" / "cmake" / "SDL2" / "sdl2-config.cmake").exists():
        log_line(log, "fetching SDL2 for MinGW (one-time)…")
        arc = base / "_dl" / "SDL2-devel-2.30.0-mingw.tar.gz"
        if not arc.exists():
            _download(SDL2_MINGW_URL, arc, log)
        _tar_extract(arc, base)
    if not (base / "zlib-mingw" / "lib" / "libz.a").exists():
        log_line(log, "building zlib for MinGW (one-time)…")
        _build_zlib_mingw(base, log)
    if not (base / "mesa-devel" / "lib" / "x64" / "libEGL.dll.a").exists() \
            or not (base / "mesa-runtime" / "x64"
                    / "libgallium_wgl.dll").exists():
        log_line(log, "fetching Mesa EGL/GLES for MinGW (one-time, "
                      "~60 MB)…")
        sevenzz = _sevenzip(log, base)
        arc = base / "_dl" / "mesa3d-26.2.5-devel-mingw.7z"
        if not arc.exists():
            _download(MESA_DEVEL_URL, arc, log)
        _extract_7z(sevenzz, arc, base / "mesa-devel", log)
        arc = base / "_dl" / "mesa3d-26.2.5-release-mingw.7z"
        if not arc.exists():
            _download(MESA_RELEASE_URL, arc, log)
        _extract_7z(sevenzz, arc, base / "mesa-runtime", log)
    if not _win_tools_ok(base):
        raise RuntimeError("Windows tools are still incomplete after "
                           "fetching — see the log above")
    log_line(log, f"windows tools: {base}")
    return base


# --------------------------------------------------------------------------
# Project scaffold / build / verify / package
# --------------------------------------------------------------------------

def _slug(title):
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s or "gb-game"


def _camel(title):
    parts = re.sub(r"[^A-Za-z0-9 ]", " ", title).split()
    name = "".join(p[:1].upper() + p[1:] for p in parts)
    return name or "GbGame"


def _exe_slug(probe, title):
    """The generated module/exe name comes from the staged ROM's file
    stem. Stage the ROM under a sanitized version of the cartridge's
    own header title (POKEMON RED -> pokemon_red) so builds get stable,
    identifier-safe names; fall back to the job title."""
    base = probe.get("header_title") or title
    s = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")
    if not s or s[0].isdigit():
        s = "gb_" + s
    return s or "game"


def _c_string(text):
    """Escape a Python string as a C string literal body (octal escapes
    for anything outside printable ASCII — unambiguous, unlike \\x)."""
    out = []
    for b in text.encode("utf-8"):
        if b == 0x22:
            out.append('\\"')
        elif b == 0x5C:
            out.append("\\\\")
        elif 0x20 <= b < 0x7F:
            out.append(chr(b))
        else:
            out.append("\\%03o" % b)
    return "".join(out)


EXTRAS_TMPL = """/* Generated by GBRecomp Tools — ROM identity hooks (extras.c).
 *
 * The generated project's CMake links this file from the project root.
 * Without it the runtime's game_defaults return no CRC, and the
 * launcher's advisory verdict reads "ROM not verified" even for the
 * exact ROM the game was built from (root-caused 2026-11-02). The
 * CRC32 below is the whole-file CRC32 of the source ROM; the runtime's
 * own SHA-256 verify remains the authoritative gate. */
#include <stdint.h>

static const uint32_t gb_valid_crcs[] = {{ 0x{crc32}U }};

const uint32_t *game_get_valid_crcs(int *out_count) {{
    if (out_count) *out_count = 1;
    return gb_valid_crcs;
}}

uint32_t game_get_expected_crc32(void) {{ return 0x{crc32}U; }}

const char *game_get_name(void) {{ return "{name}"; }}
"""

TOOLCHAIN_MINGW = """set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_C_COMPILER x86_64-w64-mingw32-gcc)
set(CMAKE_CXX_COMPILER x86_64-w64-mingw32-g++)
set(CMAKE_RC_COMPILER x86_64-w64-mingw32-windres)
set(CMAKE_FIND_ROOT_PATH /usr/x86_64-w64-mingw32)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
"""

README_TMPL = """{title} — native PC recompilation (GBRecomp)

HOW TO PLAY
  Put your own ROM dump next to {exe} (the .gb/.gbc file this build
  was made from — the game checks it and only accepts that ROM; no
  ROM is included). Run {exe}: the launcher opens first, press PLAY.

CONTROLS
  Keyboard and gamepad are set up in the launcher. In game, Esc
  opens the menu (display, graphics, audio, save states, cheats).

LINK PLAY (2-player link cable games)
  Both machines run the same build with the same ROM. In game:
  Esc -> System -> Advanced settings -> Network to turn the link on;
  one machine hosts, the other joins (LAN auto-discovery, or direct
  IP). Link TCP port 1989, discovery UDP 8766 — allow them through
  the firewall if you connect by hand.
  Setup-screen order matters: on the game's versus setup screens,
  the JOINING player should confirm their side FIRST, then the host
  starts the match. If the host starts first the joiner can be left
  sitting on the setup screen for a long time.
  If a session drops, relaunch the game on BOTH machines before
  pairing again — a host that lost its partner won't take a new one.

HARDWARE MODE
  Game Boy Color games boot in color by default, and Super Game Boy
  games boot with their SGB features on. To run a game as different
  hardware instead: Esc -> System -> Advanced settings -> Hardware
  Mode. The choice is remembered per game.

KNOWN ISSUES
  The in-game Settings -> Restart Game button currently closes the
  game instead of restarting it (framework bug). Relaunch manually.

RETROACHIEVEMENTS
{ra_block}
CREDITS
  Built with gbrecompiled by Matthew Stanley (mstan) /
  RetroPortingToolkit (retroportingtoolkit.com;
  github.com/mstan, github.com/RetroPortingToolKit), based on
  arcanite24's gb-recompiled and the GB-Recomp fork.
  Launcher: recomp-ui (MIT, (c) 2026 Matthew Stanley).
  gbrecompiled license: PolyForm Noncommercial 1.0.0.
"""


RA_BLOCK_ON = """  This build includes RetroAchievements. Put your
  retroachievements.toml next to the executable (username and
  api_token - the Connect token, in double quotes; hardcore
  unquoted) and restart the game. A login toast confirms sign-in
  and unlocks pop up in game. Unlocks register as Casual/softcore:
  RetroAchievements counts hardcore only from approved clients,
  and recomp clients are not one yet."""

RA_BLOCK_OFF = "  This build was made with RetroAchievements off."

# --------------------------------------------------------------------------
# RetroAchievements wiring
#
# The framework declares game_post_frame (runtime/include/
# game_extras.h) and ships a no-op default for it, but in the current
# checkout nothing ever calls it - only game_on_init has a call site
# (platform_sdl.cpp). The smallest honest fix lives here, in code the
# engine already post-processes: the generated <game>_main.c gets one
# call, game_post_frame(ctx), right after each completed frame (which
# is also before the platform presents the framebuffer, so the RA
# module's toast can paint into the finished frame). The generated
# CMakeLists gets a managed region that compiles bundle/ra into the
# game target. Both patches are marker-guarded and idempotent, and
# both fail loudly if the codegen anchors move.
# --------------------------------------------------------------------------

RA_CMAKE_BEGIN = "# >>> GBRecomp Tools: RetroAchievements >>>"
RA_CMAKE_END = "# <<< GBRecomp Tools: RetroAchievements <<<"

RA_CMAKE_TMPL = """{begin}
# Managed by the GBRecomp Tools engine (bundle/ra). Do not edit by
# hand; the region is rewritten on every scaffold.
if(EXISTS "${{CMAKE_CURRENT_SOURCE_DIR}}/../ra/ra_extras.cpp")
    file(GLOB_RECURSE GB_RA_RCHEEVOS_SOURCES
        "${{CMAKE_CURRENT_SOURCE_DIR}}/../ra/ra-rcheevos/src/*.c")
    list(FILTER GB_RA_RCHEEVOS_SOURCES EXCLUDE REGEX "rc_libretro\\\\.c$")
    list(FILTER GB_RA_RCHEEVOS_SOURCES EXCLUDE REGEX "rc_client_raintegration\\\\.c$")
    target_sources({exe} PRIVATE
        "${{CMAKE_CURRENT_SOURCE_DIR}}/../ra/ra_extras.cpp"
        "${{CMAKE_CURRENT_SOURCE_DIR}}/../ra/ra_http.c"
        ${{GB_RA_RCHEEVOS_SOURCES}})
    target_include_directories({exe} PRIVATE
        "${{CMAKE_CURRENT_SOURCE_DIR}}/../ra/ra-rcheevos/include"
        "${{CMAKE_CURRENT_SOURCE_DIR}}/../ra"
        "{runtime_include}")
    if(WIN32)
        target_link_libraries({exe} winhttp)
    else()
        find_package(OpenSSL REQUIRED)
        target_link_libraries({exe} OpenSSL::SSL OpenSSL::Crypto)
    endif()
{popups_def}endif()
{end}
"""


def _ra_bundle_ok():
    return ((RA_BUNDLE / "ra_extras.cpp").exists()
            and (RA_BUNDLE / "ra_http.c").exists()
            and (RA_BUNDLE / "ra-rcheevos" / "include" / "rc_client.h").exists())


def _set_cmake_ra_region(gen, exe_name, gb_dir, popups, enable):
    cmakelists = gen / "CMakeLists.txt"
    text = cmakelists.read_text()
    stripped = re.sub(
        r"(?s)" + re.escape(RA_CMAKE_BEGIN) + r".*?"
        + re.escape(RA_CMAKE_END) + r"\n?", "", text)
    if enable:
        popups_def = "" if popups else \
            f"    target_compile_definitions({exe_name} PRIVATE RA_DISABLE_POPUPS=1)\n"
        region = RA_CMAKE_TMPL.format(
            begin=RA_CMAKE_BEGIN, end=RA_CMAKE_END, exe=exe_name,
            runtime_include=str(gb_dir / "runtime" / "include").replace("\\", "/"),
            popups_def=popups_def)
        stripped = stripped.rstrip("\n") + "\n\n" + region
    if stripped != text:
        cmakelists.write_text(stripped)


def _patch_main_ra(gen, exe_name, enable, log):
    mains = sorted(gen.glob("*_main.c"))
    if len(mains) != 1:
        raise RuntimeError(f"expected exactly one generated *_main.c, "
                           f"found {[m.name for m in mains]}")
    main_path = mains[0]
    original = main_path.read_text()
    lines = [ln for ln in original.split("\n")
             if "GBRT_RA_INC" not in ln and "GBRT_RA_HOOK" not in ln]
    text = "\n".join(lines)
    if enable:
        text, n = re.subn(r'(?m)^(#include "gbrt\.h")$',
                          r'\1\n#include "game_extras.h" /* GBRT_RA_INC */',
                          text, count=1)
        if n != 1:
            raise RuntimeError("generated main has no #include \"gbrt.h\" "
                               "anchor for the RA hook include")
        text, n = re.subn(r'(?m)^(\s*)frame_index\+\+;$',
                          r'\1frame_index++;\n\1game_post_frame(ctx);'
                          r' /* GBRT_RA_HOOK */',
                          text, count=1)
        if n != 1:
            raise RuntimeError("generated main has no frame_index++ anchor "
                               "for the RA frame hook")
        log_line(log, "patched generated main: game_post_frame hook call")
    if text != original:
        main_path.write_text(text)


# --------------------------------------------------------------------------
# Hardware-mode wiring
#
# platform_sdl.h documents the contract: the recompiled main calls
# gb_platform_set_game_id between gb_platform_register_context and
# the cart's <game>_init, and the platform then seeds
# ctx->hardware_mode_pref from the cart class, applies any saved
# per-game override from runtime_prefs.ini, and ungates the in-game
# Hardware Mode combo (which keys off the active game id). But the
# emitter only writes that call into its asset-loader main template;
# the plain generated main our projects use never calls it. Result,
# root-caused on Tetris DX (2026-11-02): the pref stays unset,
# gb_context_load_rom falls back to its AUTO branch — which checks
# the ROM's SGB byte (0x146) BEFORE CGB support — and every
# dual-mode cart (CGB flag 0x80, SGB flag set; most of the GBC
# library) boots as DMG hardware with the green palette, with no
# user-facing switch to fix it.
#
# Engine fix, two halves, both using the runtime's own pref system:
#  1. Patch the generated main to make the documented call, with a
#     stable game id derived from the game (the staged ROM's slug —
#     the same string the save id uses).
#  2. Stage a runtime_prefs.ini beside the exe pinning the mode the
#     cart header asks for, so the shipped default does not depend
#     on the framework's resolution order:
#       CGB flag 0x80 or 0xC0  -> game.<id>.hardware_mode=cgb
#       SGB flag on a DMG cart -> game.<id>.hardware_mode=sgb
#         (those carts get the SGB engine via AUTO today; a concrete
#         DMG pref would silently take it away)
#     Plain DMG carts get no line: the platform default resolves DMG.
#     The player can still flip the mode in-game; the choice saves
#     back to the same per-game key.
# The main patch is marker-guarded and idempotent (same pattern as
# the RA patch) and fails loudly if the codegen anchor moves.
# --------------------------------------------------------------------------

GAME_ID_MARKER = "GBRT_GAME_ID"


def _patch_main_game_id(gen, game_id, log):
    mains = sorted(gen.glob("*_main.c"))
    if len(mains) != 1:
        raise RuntimeError(f"expected exactly one generated *_main.c, "
                           f"found {[m.name for m in mains]}")
    main_path = mains[0]
    original = main_path.read_text()
    lines = [ln for ln in original.split("\n") if GAME_ID_MARKER not in ln]
    text = "\n".join(lines)
    text, n = re.subn(
        r'(?m)^(\s*)gb_platform_register_context\(ctx\);$',
        r'\1gb_platform_register_context(ctx);\n'
        r'\1gb_platform_set_game_id(ctx, "' + game_id + r'");'
        r' /* ' + GAME_ID_MARKER + ' */',
        text, count=1)
    if n != 1:
        raise RuntimeError("generated main has no gb_platform_register_context "
                           "anchor for the game-id call")
    if text != original:
        main_path.write_text(text)
    log_line(log, f"patched generated main: gb_platform_set_game_id "
                  f"(game id \"{game_id}\")")


def _hardware_mode_pref(probe):
    """The runtime_prefs.ini hardware_mode value a cart's header asks
    for, or None when the platform default already resolves right."""
    if probe["cgb_flag"] in (0x80, 0xC0):
        return "cgb"
    if probe.get("sgb"):
        return "sgb"
    return None


def _wire_ra(job, proj, gen, exe_name, gb_dir, enable, log):
    """Copy the RA bundle into the project and patch the generated
    CMakeLists + main. With enable=False the patches are stripped, so
    flipping the job's ra flag off cleanly unmakes an RA build."""
    if enable:
        dest = proj / "ra"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(RA_BUNDLE, dest)
        log_line(log, "RA bundle copied into the project (bundle/ra)")
    _set_cmake_ra_region(gen, exe_name, gb_dir,
                         popups=job.get("ra_popups", True), enable=enable)
    _patch_main_ra(gen, exe_name, enable, log)


def scaffold_project(job, probe, gb_dir, gbrecomp, log, ra_on=False):
    out_dir = Path(job["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    title = job.get("title") or probe["title"]
    camel = _camel(title)
    exe_slug = _exe_slug(probe, title)
    proj = out_dir / (camel + "-src")
    proj.mkdir(parents=True, exist_ok=True)
    ext = ".gbc" if probe["cgb"] else ".gb"
    staged_rom = proj / (exe_slug + ext)
    shutil.copy2(job["rom"], staged_rom)
    gen = proj / "gen"
    if job.get("regen") and gen.exists():
        shutil.rmtree(gen)
    if not (gen / "CMakeLists.txt").exists():
        log_line(log, "generating project with gbrecomp (auto-analysis)…")
        _run([str(gbrecomp), str(staged_rom), "-o", str(gen)], log,
             cwd=proj, timeout=1800)
    # The generated CMakeLists hardcodes GBRT_DIR relative to wherever
    # gbrecomp ran (it expects to live inside the framework tree).
    # Point it at the real runtime.
    cmakelists = gen / "CMakeLists.txt"
    text = cmakelists.read_text()
    runtime = str(gb_dir / "runtime").replace("\\", "/")
    new_text, n = re.subn(r'set\(GBRT_DIR\s+"[^"]*"\)',
                          f'set(GBRT_DIR "{runtime}")', text, count=1)
    if n != 1:
        raise RuntimeError("generated CMakeLists has no GBRT_DIR line to patch")
    if new_text != text:
        cmakelists.write_text(new_text)
        log_line(log, f"patched GBRT_DIR -> {runtime}")
    exe_name = _generated_exe_name(gen, exe_slug)
    # Identity hooks at the project root (one level up from gen/).
    crc = probe["crc32"].upper()
    (proj / "extras.c").write_text(EXTRAS_TMPL.format(
        crc32=crc, name=_c_string(title)))
    log_line(log, f"extras.c: CRC hooks for 0x{crc}, name \"{title}\"")
    # RetroAchievements: bundle copy + generated CMake/main patches.
    _wire_ra(job, proj, gen, exe_name, gb_dir, ra_on, log)
    # Hardware mode: register the game id with the platform so the
    # per-game pref system (and the in-game Hardware Mode combo) is
    # live and CGB carts boot as CGB. Independent of the RA flag.
    _patch_main_game_id(gen, exe_slug, log)
    # Box art: the generated CMake stages <proj>/recomp/launcher/
    # boxart.tga next to the exe at build time.
    cover = fetch_cover(title, cgb=probe["cgb"], log=log,
                        sgb=probe["sgb"], cgb_flag=probe["cgb_flag"])
    if cover:
        tga = proj / "recomp" / "launcher" / "boxart.tga"
        tga.parent.mkdir(parents=True, exist_ok=True)
        if _png_to_tga(cover, tga):
            log_line(log, f"box art staged: {tga}")
        else:
            log_line(log, "WARNING: box art PNG fetched but TGA "
                          "conversion failed; launcher will show no art")
    (proj / "tc-mingw.cmake").write_text(TOOLCHAIN_MINGW)
    return proj, camel, title, exe_name


def _generated_exe_name(gen, fallback):
    mains = sorted(gen.glob("*_main.c"))
    if mains:
        return mains[0].name[:-len("_main.c")]
    return fallback


def build_target(proj, exe_name, target, jobs, ui_dir, log):
    gen = proj / "gen"
    if target == "linux":
        bdir = gen / "build-linux"
        cmd = ["cmake", "-S", str(gen), "-B", str(bdir), "-G", "Ninja",
               "-DGBRT_BOOT_IMPLEMENTATION=HLE",
               "-DRECOMP_UI_ROOT=" + str(ui_dir)]
    else:
        bdir = gen / "build-win"
        tools = ensure_win_tools(log)
        sdl2_prefix = tools / "SDL2-2.30.0" / "x86_64-w64-mingw32"
        cmd = ["cmake", "-S", str(gen), "-B", str(bdir), "-G", "Ninja",
               "-DCMAKE_TOOLCHAIN_FILE=" + str(proj / "tc-mingw.cmake"),
               "-DGBRT_BOOT_IMPLEMENTATION=HLE",
               "-DRECOMP_UI_ROOT=" + str(ui_dir),
               "-DCMAKE_PREFIX_PATH=" + str(sdl2_prefix),
               "-DZLIB_INCLUDE_DIR=" + str(tools / "zlib-mingw" / "include"),
               "-DZLIB_LIBRARY=" + str(tools / "zlib-mingw" / "lib" / "libz.a"),
               "-DCMAKE_C_FLAGS=-I" + str(tools / "mesa-devel" / "include"),
               "-DCMAKE_CXX_FLAGS=-I" + str(tools / "mesa-devel" / "include"),
               "-DCMAKE_EXE_LINKER_FLAGS=-L" + str(tools / "mesa-devel" / "lib" / "x64")]
    _run(cmd, log, timeout=1800)
    _run(["cmake", "--build", str(bdir), "-j", str(jobs)], log, timeout=3600)
    exe = bdir / (exe_name + (".exe" if target == "windows" else ""))
    return bdir, exe


def verify_build(bdir, exe, gen):
    if not exe.exists() or exe.stat().st_size < 1_000_000:
        return {"ok": False, "reason": f"binary missing or too small: {exe}"}
    if not list(gen.glob("*_rom.c")):
        return {"ok": False, "reason": "generated ROM sources not found"}
    if not (bdir / "assets" / "img").is_dir():
        return {"ok": False, "reason": "launcher assets not staged in build dir"}
    return {"ok": True, "size": exe.stat().st_size}


def _mingw_dll(name):
    out = subprocess.run(["x86_64-w64-mingw32-g++", "-print-file-name=" + name],
                         capture_output=True, text=True, timeout=15)
    p = Path(out.stdout.strip())
    return p if p.exists() else None


# Windows DLLs the GB runtime needs beside the exe: SDL2, the Mesa
# EGL/GLES pair + its software-rendering backend, and the MinGW
# runtimes (stripped — an unstripped libstdc++-6.dll alone is 26.8 MB).
def _win_dlls_fixed():
    t = tools_dir()
    return [
        ("SDL2.dll", t / "SDL2-2.30.0" / "x86_64-w64-mingw32" / "bin" / "SDL2.dll"),
        ("libEGL.dll", t / "mesa-runtime" / "x64" / "libEGL.dll"),
        ("libGLESv2.dll", t / "mesa-runtime" / "x64" / "libGLESv2.dll"),
        ("libgallium_wgl.dll", t / "mesa-runtime" / "x64" / "libgallium_wgl.dll"),
    ]


WIN_DLLS_MINGW = ["libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll"]


def package(job, probe, camel, title, target, exe, bdir, log, ra_on=False,
            game_id=None):
    out_dir = Path(job["output_dir"])
    stage = out_dir / (camel + "-pkg-" + target)
    if stage.exists():
        shutil.rmtree(stage)
    game_dir = stage / camel
    game_dir.mkdir(parents=True)
    exe_name = camel + (".exe" if target == "windows" else "")
    shutil.copy2(exe, game_dir / exe_name)
    # The launcher loads its runtime assets (fonts, brand art, verdict
    # icons, and the game's box art as assets/img/boxart.tga) from an
    # assets/ folder beside the exe — it only works when assets/ ships.
    assets_src = bdir / "assets"
    if assets_src.is_dir():
        shutil.copytree(assets_src, game_dir / "assets")
    else:
        log_line(log, "WARNING: no assets/ in build dir; package will "
                      "show the placeholder launcher")
    # Per-game hardware-mode pref: runtime_prefs.ini lives beside the
    # exe (the runtime's state dir) and gb_platform_set_game_id applies
    # game.<id>.hardware_mode over its cart-class default. Pin the
    # mode the cart header asks for (see _hardware_mode_pref).
    hw_mode = _hardware_mode_pref(probe)
    if hw_mode and game_id:
        (game_dir / "runtime_prefs.ini").write_text(
            f"game.{game_id}.hardware_mode={hw_mode}\n")
        log_line(log, f"staged runtime_prefs.ini: game.{game_id}."
                      f"hardware_mode={hw_mode}")
    if target == "windows":
        for name, src in _win_dlls_fixed():
            if src.exists():
                shutil.copy2(src, game_dir / name)
            else:
                log_line(log, f"WARNING: Windows DLL missing: {src}")
        for dll in WIN_DLLS_MINGW:
            src = _mingw_dll(dll)
            if src:
                dst = game_dir / dll
                shutil.copy2(src, dst)
                try:
                    subprocess.run(["x86_64-w64-mingw32-strip", str(dst)],
                                   capture_output=True, timeout=60)
                except Exception:  # noqa: BLE001
                    pass
            else:
                log_line(log, f"WARNING: MinGW runtime DLL not found: {dll}")
    if ra_on:
        if job.get("ra_toml") and Path(job["ra_toml"]).exists():
            shutil.copy2(job["ra_toml"],
                         game_dir / "retroachievements.toml")
        example = RA_BUNDLE / "retroachievements.toml.example"
        if example.exists():
            shutil.copy2(example,
                         game_dir / "retroachievements.toml.example")
    cover = ART_CACHE / (title + ".png")
    if cover.exists():
        art_dir = game_dir / "artwork" / title
        art_dir.mkdir(parents=True)
        shutil.copy2(cover, art_dir / "front.png")
    (game_dir / "README.txt").write_text(README_TMPL.format(
        title=title, exe=exe_name,
        ra_block=RA_BLOCK_ON if ra_on else RA_BLOCK_OFF))
    zip_path = out_dir / (camel + ("-Windows.zip" if target == "windows"
                                   else "-Linux.zip"))
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(game_dir.rglob("*")):
            if f.is_file():
                info = zipfile.ZipInfo(str(f.relative_to(stage)))
                info.compress_type = zipfile.ZIP_DEFLATED
                if f.name == exe_name:
                    info.external_attr = 0o755 << 16  # keep the exec bit
                z.writestr(info, f.read_bytes())
    shutil.rmtree(stage)
    log_line(log, f"package: {zip_path} ({zip_path.stat().st_size} bytes)")
    return zip_path


def run_job(job, log=None):
    probe = probe_rom(job["rom"])
    log_line(log, f"probe: header \"{probe['header_title']}\" MBC "
                  f"{probe['mbc']} ROM {probe['rom_size_kb']}K RAM "
                  f"{probe['ram_size_kb']}K CGB={probe['cgb']} crc32 "
                  f"{probe['crc32']} sha256 {probe['sha256'][:12]}…")
    if probe["needs_review"] and not job.get("force"):
        log_line(log, "flagged needs-review (hand-seeding); set force "
                      "to try plain auto-analysis anyway")
        return {"status": "needs_review", "probe": probe}
    ra_on = bool(job.get("ra", True))
    if ra_on and not _ra_bundle_ok():
        log_line(log, "WARNING: RetroAchievements requested but the RA "
                      "bundle is missing or incomplete (bundle/ra/) - "
                      "this package ships WITHOUT RetroAchievements.")
        ra_on = False
    if ra_on:
        log_line(log, "RetroAchievements: on (bundle/ra)")
    gb_dir, gbrecomp, ui_dir = ensure_frameworks(log)
    proj, camel, title, exe_name = scaffold_project(
        job, probe, gb_dir, gbrecomp, log, ra_on=ra_on)
    results = {}
    failed = {}
    for target in job.get("targets", ["linux", "windows"]):
        try:
            log_line(log, f"building {target}…")
            bdir, exe = build_target(proj, exe_name, target,
                                     int(job.get("jobs", 2)), ui_dir, log)
            v = verify_build(bdir, exe, proj / "gen")
            if not v["ok"]:
                raise RuntimeError(f"verify failed for {target}: {v['reason']}")
            log_line(log, f"verify {target}: ok ({v['size']} bytes)")
            results[target] = str(package(job, probe, camel, title, target,
                                          exe, bdir, log, ra_on=ra_on,
                                          game_id=_exe_slug(probe, title)))
        except Exception as e:  # noqa: BLE001 — one target must not
            # sink the other: a packaged Linux zip stays packaged even
            # when the Windows build fails (2026-11-02 field run: the
            # whole queue showed FAILED over a Windows-only fault
            # while every Linux package had in fact been produced).
            failed[target] = str(e)
            log_line(log, f"{target} FAILED: {e}")
    if not results:
        raise RuntimeError("; ".join(
            f"{t}: {m}" for t, m in failed.items()))
    out = {"status": "done", "probe": probe, "packages": results}
    if failed:
        out["failed_targets"] = failed
        log_line(log, "finished with target failures: "
                      + ", ".join(sorted(failed)))
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "toolbox":
        print(json.dumps(toolbox(), indent=2))
        return 0
    if cmd == "probe":
        rom = argv[argv.index("--rom") + 1]
        print(json.dumps(probe_rom(rom), indent=2))
        return 0
    if cmd == "run":
        job_path = argv[argv.index("--job") + 1]
        job = json.loads(Path(job_path).read_text())
        result = run_job(job)
        print(json.dumps({"RESULT": result["status"], **result}, indent=2))
        return 0 if result["status"] == "done" else 1
    print("unknown command: " + cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
