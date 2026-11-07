# GBRecomp Tools v1.0.1 — release notes (DRAFT — not yet published)

## What was wrong

Converting a large Game Boy Color game could hang forever. Duane's GBC
run reached **Alone in the Dark – The New Nightmare (4 MB)** and sat
there overnight: the app window open, the machine fine, no error, no
progress.

Root cause, in two parts:

1. **The framework compiler's default code scan explodes on large
   ROMs.** `gbrecomp` (from mstan's gbrecompiled framework, used
   as-is) analyses a ROM by scanning for code. On big, data-heavy
   cartridges the scan's cost grows far faster than the ROM size:
   rejected candidate bytes are re-examined on every pass, and the
   passes keep finding one more false "function" in the game's data,
   so it effectively never finishes. Measured: a 32 KB game converts
   in under a second; a 2 MB game's analysis alone took 7 minutes
   25 seconds (and hallucinated 17,059 "functions"); the 4 MB games
   never finished at all — one CPU core at 100%, no output, forever.
2. **The tool's timeout couldn't fire on a silent child.** The engine
   only started its timeout clock after the child's output stream
   ended — and the scan prints nothing — so instead of failing the
   item after a deadline, the conversion hung silently with it.

## What v1.0.1 changes

- **ROMs of 2 MB and larger now skip the aggressive scan**
  (`gbrecomp --no-scan`). The scan is a code-discovery aid; on large
  ROMs it costs unbounded time and adds mostly false positives. The
  conversion log says so plainly when it happens:
  `ROM ≥2 MB: aggressive scan skipped`. Games under 2 MB are
  completely unchanged — the scan still runs exactly as before.
- **Every subprocess now has a real wall-clock timeout** that fires
  even when the child produces no output. On timeout the child (and
  anything it spawned) is killed, the queue item is marked **FAILED**
  with the stage named (e.g. `generate stage (gbrecomp): timed out
  after 1800 s`), the end-of-run summary reports the failure, and the
  batch continues with the next game. A failed item can no longer sit
  forever or read as done.
- The command-line runner now reports `RESULT: failed` and exits
  non-zero on a failed job instead of dying with a traceback.

No framework code was forked or patched — both fixes live in this
tool's engine, and `gbrecomp` is still downloaded and used as-is.

## Who was affected

In Duane's own GBC census (467 titles): **27 titles at 4 MB** and
**71 titles at 2 MB** — 98 games that would have crawled or hung,
including Alone in the Dark, both Harry Potter games, Shantae,
Dragon Warrior III, Grand Theft Auto, Donkey Kong Country, both
Austin Powers games, Perfect Dark, both Raymans and all three Tomb
Raiders. The Game Boy (non-Color) side has no ROMs ≥2 MB, which is
why the GB library converted cleanly and only the GBC run hit this.

## Verification (all from a clean-room copy of this exact zip)

- **Alone in the Dark – The New Nightmare (4 MB)** — the game that
  hung overnight: full conversion completes; booted through the
  launcher to the title screen, new game, story intro, and
  player-controlled gameplay (character walks in the first room).
- **Harry Potter and the Chamber of Secrets (4 MB)** — full
  conversion in 62 seconds total; booted to title, menu, New Game,
  opening scene and free-roam gameplay with the character moving.
- **Alice in Wonderland (2 MB)** — full conversion in 62 seconds
  total (before the fix, the analysis step alone took 7m25s).
- **1942 (1 MB)** — converts with the scan still on (log proves no
  `--no-scan` was passed), exactly as before.
- **Tetris (32 KB)** — converts and plays (menu → game → piece moved
  on input), running 100% native code.
- **Timeout path** — a deliberately silent, hung compiler child is
  killed at the deadline, its item fails with the stage named, and
  the next item in the batch converts normally.
- **Windows** — cross-build verified: a Windows package builds and
  passes the tool's own verification (Tetris, `.exe` + `SDL2.dll`).
  The Windows build was **compile-verified only**; it has not been
  run on a Windows machine.

## Still owed (honest list)

- **RetroAchievements login + a real unlock** on Duane's machines —
  RA sign-in is unreliable from the build VM, so it is not claimed
  verified here. (v1.0.1 changes nothing about RA.)
- **Connected link play** — verified working back in 2026 with
  Tetris on v1.0.0 packages over Duane's LAN; no new proof is claimed
  for v1.0.1, and a connected match from these packages is still
  owed on his machines.
- **Windows runtime test** on a real Windows machine.

## Sibling tools — same timeout defect found (audit only)

The silent-child timeout flaw (defect 2 above) also exists in the
**NES, SNES, GBA and N64** converter tools — the same code pattern
was copied between tools. Genesis, SMS/Game Gear and Virtual Boy
tools use a real deadline and are **not** affected. Those tools are
NOT fixed by this release; they are listed here so the follow-up
fixes are on the record. (Their hang risk is lower in practice —
none of them drives a scan with this one's cost curve — but a silent
child would hang them the same way.)
