# GBRecomp Tools

Converts your Game Boy / Game Boy Color ROMs into native PC games —
Linux and Windows executables with a launcher, box art, save states,
and link-cable play over the network — using
[gbrecompiled](https://github.com/mstan/gbrecompiled).

RetroAchievements are on by default (login toast + in-game pop-ups
via the bundled module in `bundle/ra/`; turn them off in Settings,
or set a job's `ra` to false, and pass `ra_toml` to ship a filled
retroachievements.toml in the package).

## Installing / first run

Unzip `GBRecompTools.zip` anywhere and run:

```sh
./gb-recomp-tools.sh
```

The first run creates a Python environment inside the folder and
installs the app's dependencies, then opens the converter window:
add your ROMs, press **Run Queue**, and pick up the packaged games
from the output folder. The very first conversion also downloads
and builds the gbrecompiled framework (and the recomp-ui launcher)
into the app's `framework-cache/` — that happens once and takes a
while; later conversions reuse it. You need a normal build toolbox
(cmake, ninja, compilers, SDL2 dev, MinGW for the Windows builds);
the app's Convert tab shows what's missing and can install the
packages for you.

## Graphical front end

`./gb-recomp-tools.sh` with no arguments opens the GUI: the Convert
tab probes every ROM you add from its cartridge header (type,
ROM/RAM size, Color support), shows cover art and the toolbox
status, and converts the queue one game at a time; the Help tab
explains link play, RetroAchievements and the rest.

## Command line

```sh
./gb-recomp-tools.sh                         # open the GUI
./gb-recomp-tools.sh toolbox                 # check the build toolbox
./gb-recomp-tools.sh probe --rom game.gb     # inspect a ROM header
./gb-recomp-tools.sh run --job job.json      # convert one game
```

(Or call `python3 app/engine.py ...` directly if Pillow is already
installed system-wide.)

A job file looks like:

```json
{"rom": "/path/to/Tetris (JUE) (V1.1) [!].gb",
 "title": "Tetris (JUE) (V1.1) [!]",
 "output_dir": "/path/to/output",
 "targets": ["linux", "windows"],
 "jobs": 2}
```

Each run produces `<Title>-Linux.zip` and/or `<Title>-Windows.zip`
in the output folder, plus a `<Title>-src` project folder. The
packages contain **no ROM and no BIOS** — the recompiler uses an HLE
boot implementation, and every player supplies their own ROM dump,
which sits next to the executable.

## How a conversion works

1. **Probe** — parse the cartridge header (title, MBC type, ROM/RAM
   size, CGB flag, checksums) and fingerprint the ROM (CRC32 +
   SHA-256).
2. **Generate** — run `gbrecomp` (plain auto-analysis) and patch the
   generated project's runtime path.
3. **Scaffold** — write an `extras.c` with the ROM's CRC32 identity
   hooks (without them the launcher falsely reports "ROM not
   verified") and stage the box art fetched from libretro-thumbnails
   (GitHub mirror first, canonical host second, No-Intro name
   variants tried).
4. **Build** — CMake + Ninja for Linux; MinGW cross-build for
   Windows (SDL2 + zlib + Mesa EGL/GLES, fetched automatically —
   see Toolbox).
5. **Verify + package** — the executable, the launcher `assets/`
   tree, `artwork/<title>/front.png`, and a README with the
   link-play notes (TCP 1989 / UDP 8766, joiner confirms first,
   relaunch both after a drop) go into the zip.

## Toolbox

Native: cmake, ninja, gcc/g++, git, MinGW gcc/g++, SDL2 dev
(`libsdl2-dev`), Python ≥ 3.11, Pillow. The Windows cross-build
support files (SDL2 MinGW devel, a MinGW zlib built from source,
Mesa EGL/GLES headers + runtime DLLs) are downloaded and staged
automatically into the app's own `tools/` folder on the first
Windows build — a one-time ~70 MB fetch; nothing to set up by hand.
Run `./gb-recomp-tools.sh toolbox` to see what's present.

## Credits

Built on **gbrecompiled** by Matthew Stanley (mstan) /
RetroPortingToolkit (retroportingtoolkit.com;
github.com/mstan, github.com/RetroPortingToolKit), based on
arcanite24's gb-recompiled and the GB-Recomp fork. Launcher:
recomp-ui (MIT, © 2026 Matthew Stanley). gbrecompiled is licensed
under the PolyForm Noncommercial License 1.0.0.
