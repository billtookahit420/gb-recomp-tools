#!/usr/bin/env python3
"""GBRecomp Tools — convert Game Boy / Game Boy Color ROMs to native
PC games.

Pick ROMs -> they are probed instantly from their cartridge headers ->
Run Queue converts them one at a time (gbrecompiled: launcher UI, box
art, RetroAchievements with in-game pop-ups, link-cable play) and
packages Linux + Windows zips. Failures are logged and skipped, never
fatal to the queue. Games whose ROMs need hand-seeding are flagged
"needs review" instead of being guessed at.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine  # noqa: E402

from PySide6.QtCore import Qt, QThread, Signal  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

APP_DIR = Path(__file__).resolve().parent.parent
SETTINGS_FILE = APP_DIR / "settings.json"

STATUS_COL = 6

HELP_TEXT = """\
GBRecomp Tools — turn your own Game Boy / Game Boy Color cartridges'
ROM dumps into native PC games: no emulator, the game's code is
recompiled to a real executable with a launcher, save states, box
art, link-cable play and RetroAchievements.

HOW IT WORKS
  Add .gb / .gbc files (or a whole folder). Each ROM is probed
  instantly from its cartridge header: title, cartridge type (MBC),
  ROM/RAM size, Game Boy Color support, region. Press Run Queue and
  each game is converted one at a time and packaged as
  <Game>-Linux.zip and <Game>-Windows.zip in the output folder.
  A game that fails is marked and the queue moves on.

THE GAMES THEMSELVES — SUPPLY YOUR OWN ROM
  The package contains the executable, its launcher assets and a
  README. It does NOT contain the ROM: put your own dump of the
  exact cartridge next to the executable and the launcher checks
  its fingerprint before it will play. Keep the assets/ folder
  next to the executable — the launcher's art, fonts and icons
  live there.

NO BIOS NEEDED
  These builds boot with a high-level (HLE) boot implementation,
  so no Nintendo boot ROM / BIOS file is needed or included.
  Nothing to hunt down, nothing to configure.

COVER ART
  Box art is fetched automatically from the libretro-thumbnails
  project (Game Boy and Game Boy Color sets) and baked into the
  launcher, and is also included in each package under artwork/.
  A few games have no art under their exact name; those launchers
  just show the placeholder art.

RETROACHIEVEMENTS
  With RA on (default), each game includes RetroAchievements: put
  your filled retroachievements.toml next to the game executable
  and restart the game — you'll see a "Logged in" toast in game,
  and achievement unlocks pop up the same way. The toml holds your
  username and your api_token — use the token from
  retroachievements.org under Settings (the Connect token), in
  double quotes; hardcore unquoted. The Settings pop-ups checkbox
  hides both the toast and the pop-ups. Use the RA toml picker in
  Settings to have your filled toml included in every package
  automatically. Note that RetroAchievements counts hardcore only
  from approved clients; recomp clients currently earn
  Casual/softcore unlocks.

LINK PLAY (link cable over the network)
  Two-player link-cable games can be played between two machines
  running the same build with the same ROM. In game: Esc ->
  System -> Advanced settings -> Network to turn the link on; one
  machine hosts, the other joins. Machines on the same network
  find each other automatically (discovery UDP 8766); a direct
  connection uses link TCP port 1989 — allow both through the
  firewall if you connect by hand.
  Setup-screen order matters: on the game's versus setup screens,
  the JOINING player should confirm their side FIRST, then the
  host starts the match. If the host starts first, the joiner can
  be left sitting on the setup screen for a long time.
  If a session drops, relaunch the game on BOTH machines before
  pairing again — a host that lost its partner won't take a new
  one.
  Internet play: there is no public lobby service for this. Play
  over a private network tool (e.g. Tailscale) as if it were a
  LAN, connect directly to a host who has forwarded TCP 1989, or
  use a rendezvous server if one is ever set up.

KNOWN FRAMEWORK BUG
  The in-game Settings -> Restart Game button currently closes the
  game instead of restarting it. Relaunch the game manually; the
  bug is in the framework, not in your build.

"NEEDS REVIEW"
  Some titles need hand-seeded entry points that plain
  auto-analysis misses. If such a title is identified, the queue
  flags it "needs review" and skips it instead of shipping a
  broken build.

CREDITS
  Built on gbrecompiled by Matthew Stanley (mstan), part of the
  RetroPortingToolkit (retroportingtoolkit.com;
  github.com/mstan, github.com/RetroPortingToolKit), based on
  arcanite24's gb-recompiled and the GB-Recomp fork.
  gbrecompiled license: PolyForm Noncommercial 1.0.0.
  Launcher: recomp-ui (MIT, (c) 2026 Matthew Stanley).
"""


class Worker(QThread):
    log = Signal(str)
    status = Signal(int, str)
    finished_all = Signal()

    def __init__(self, items, settings):
        super().__init__()
        self.items = items          # list of (row, job-dict)
        self.settings = settings

    def run(self):
        done = partial = review = 0
        failures = []
        for row, job in self.items:
            self.status.emit(row, "converting…")
            try:
                result = engine.run_job(job, log=self.log.emit)
                if result["status"] == "needs_review":
                    self.status.emit(row, "needs review")
                    review += 1
                elif result.get("failed_targets"):
                    ok = ", ".join(sorted(result.get("packages", {})))
                    self.status.emit(row, f"done ({ok} only)")
                    self.log.emit(
                        f"PARTIAL {job.get('title')}: packaged {ok}; "
                        "failed: " + "; ".join(
                            f"{t}: {m}" for t, m in
                            result["failed_targets"].items()))
                    partial += 1
                else:
                    self.status.emit(row, "done")
                    done += 1
            except Exception as e:  # noqa: BLE001
                self.log.emit(f"FAILED {job.get('title')}: {e}")
                self.status.emit(row, "failed")
                failures.append((job.get("title"), str(e)))
        # End-of-run summary: a failed item must be impossible to
        # miss — it is counted and named here, with the stage that
        # failed, not just left as a line in the scrollback.
        total = len(self.items)
        self.log.emit(
            f"Summary: {total} item(s): {done} done, {partial} "
            f"partial, {review} needs review, {len(failures)} failed")
        for title, err in failures:
            self.log.emit(f"Summary FAILED: {title}: {err}")
        self.finished_all.emit()


class CoverWorker(QThread):
    done = Signal(str, object)

    def __init__(self, title, cgb=False, sgb=False, cgb_flag=None):
        super().__init__()
        self.title = title
        self.cgb = cgb
        self.sgb = sgb
        self.cgb_flag = cgb_flag

    def run(self):
        try:
            p = engine.fetch_cover(self.title, cgb=self.cgb,
                                   sgb=self.sgb, cgb_flag=self.cgb_flag)
        except Exception:  # noqa: BLE001
            p = None
        self.done.emit(self.title, p)


class InstallWorker(QThread):
    done = Signal(bool)

    def __init__(self, packages):
        super().__init__()
        self.packages = packages

    def run(self):
        try:
            subprocess.run(["pkexec", "apt-get", "update"], check=False)
            r = subprocess.run(["pkexec", "apt-get", "install", "-y"]
                               + self.packages, check=False)
            self.done.emit(r.returncode == 0)
        except Exception:  # noqa: BLE001
            self.done.emit(False)


def _cgb_label(probe):
    if probe.get("cgb_only"):
        return "GBC only"
    if probe.get("cgb"):
        return "GB+GBC"
    return "GB"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"GBRecomp Tools {engine.APP_VERSION}")
        self.resize(1180, 760)
        self.items = []             # list of probe dicts (+ status)
        self.worker = None
        # Live cover-fetch threads. Every worker stays referenced
        # here until it finishes: the previous code kept only the
        # newest worker in self.cover_worker, so changing the
        # selection while a fetch was running dropped the last
        # reference to a still-running QThread and Qt aborted the
        # app ("QThread: Destroyed while thread is still running" —
        # Duane's scroll/add crashes, reproduced offscreen
        # 2026-11-02).
        self._cover_workers = set()
        self._cover_wanted = None   # key of the cover the panel wants
        self._build_ui()
        self._load_settings()
        self.refresh_toolbox()

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        tabs = QTabWidget()
        tabs.addTab(self._convert_tab(), "Convert")
        help_edit = QPlainTextEdit(HELP_TEXT)
        help_edit.setReadOnly(True)
        tabs.addTab(help_edit, "Help")
        self.setCentralWidget(tabs)

    def _convert_tab(self):
        root = QWidget()
        v = QVBoxLayout(root)
        split = QSplitter(Qt.Horizontal)

        left = QWidget()
        lv = QVBoxLayout(left)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Title", "Cart (MBC)", "ROM", "RAM", "CGB", "Region",
             "Status"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.itemSelectionChanged.connect(self.show_selected)
        lv.addWidget(self.table)
        btns = QHBoxLayout()
        for label, fn in [("Add ROMs…", self.add_roms),
                          ("Add Folder…", self.add_folder),
                          ("Remove", self.remove_selected),
                          ("Clear", self.clear_queue)]:
            b = QPushButton(label)
            b.clicked.connect(fn)
            btns.addWidget(b)
        lv.addLayout(btns)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.addWidget(self._probe_box())
        rv.addWidget(self._settings_box())
        rv.addStretch(1)
        split.addWidget(right)
        split.setSizes([640, 480])
        v.addWidget(split, 3)

        run_row = QHBoxLayout()
        self.toolbox_label = QLabel("Toolbox: …")
        run_row.addWidget(self.toolbox_label)
        self.install_btn = QPushButton("Install missing packages")
        self.install_btn.clicked.connect(self.install_packages)
        run_row.addWidget(self.install_btn)
        run_row.addStretch(1)
        self.run_btn = QPushButton("Run Queue")
        self.run_btn.clicked.connect(self.run_queue)
        run_row.addWidget(self.run_btn)
        v.addLayout(run_row)

        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        v.addWidget(self.log_edit, 2)
        root._split = split
        return root

    def _probe_box(self):
        box = QGroupBox("ROM details")
        form = QFormLayout(box)
        self.p_title = QLabel("—")
        self.p_cart = QLabel("—")
        self.p_sizes = QLabel("—")
        self.p_cgb = QLabel("—")
        self.p_region = QLabel("—")
        self.p_crc = QLabel("—")
        self.p_cover = QLabel("")
        self.p_cover.setAlignment(Qt.AlignCenter)
        form.addRow("Header title:", self.p_title)
        form.addRow("Cartridge:", self.p_cart)
        form.addRow("ROM / RAM:", self.p_sizes)
        form.addRow("Colour:", self.p_cgb)
        form.addRow("Region:", self.p_region)
        form.addRow("CRC32:", self.p_crc)
        form.addRow(self.p_cover)
        return box

    def _settings_box(self):
        box = QGroupBox("Settings (apply to the whole queue)")
        form = QFormLayout(box)
        self.s_ra = QCheckBox("RetroAchievements")
        self.s_ra.setChecked(True)
        self.s_popups = QCheckBox("Achievement pop-ups + login toast")
        self.s_popups.setChecked(True)
        form.addRow(self.s_ra)
        form.addRow(self.s_popups)
        toml_row = QHBoxLayout()
        self.s_toml = QLineEdit()
        pick = QPushButton("Pick…")
        pick.clicked.connect(lambda: self._pick_file(self.s_toml,
                                                     "TOML (*.toml)"))
        toml_row.addWidget(self.s_toml)
        toml_row.addWidget(pick)
        form.addRow("RA toml to include:", toml_row)
        tgt_row = QHBoxLayout()
        self.s_linux = QCheckBox("Linux")
        self.s_linux.setChecked(True)
        self.s_windows = QCheckBox("Windows")
        self.s_windows.setChecked(True)
        tgt_row.addWidget(self.s_linux)
        tgt_row.addWidget(self.s_windows)
        form.addRow("Build targets:", tgt_row)
        self.s_jobs = QSpinBox()
        self.s_jobs.setRange(1, 16)
        self.s_jobs.setValue(2)
        form.addRow("Build jobs:", self.s_jobs)
        out_row = QHBoxLayout()
        self.s_out = QLineEdit(str(APP_DIR / "output"))
        pick_o = QPushButton("Pick…")
        pick_o.clicked.connect(lambda: self._pick_dir(self.s_out))
        out_row.addWidget(self.s_out)
        out_row.addWidget(pick_o)
        form.addRow("Output folder:", out_row)
        return box

    # ------------------------------------------------------------ util
    def _pick_file(self, edit, filt):
        p, _ = QFileDialog.getOpenFileName(self, "Pick a file", "", filt)
        if p:
            edit.setText(p)

    def _pick_dir(self, edit):
        p = QFileDialog.getExistingDirectory(self, "Pick a folder")
        if p:
            edit.setText(p)

    def log_line(self, msg):
        self.log_edit.appendPlainText(str(msg))

    def refresh_toolbox(self):
        try:
            t = engine.toolbox()
            if t["ready"]:
                self.toolbox_label.setText("Toolbox: ready")
                self.install_btn.setEnabled(False)
            else:
                self.toolbox_label.setText(
                    "Toolbox: missing " + ", ".join(t["missing_apt"]))
                self.install_btn.setEnabled(True)
            self._missing = t["missing_apt"]
        except Exception as e:  # noqa: BLE001
            self.toolbox_label.setText(f"Toolbox: check failed ({e})")
            self._missing = []

    def install_packages(self):
        if not getattr(self, "_missing", None):
            return
        self.install_btn.setEnabled(False)
        self.log_line("Installing: " + ", ".join(self._missing))
        self._inst = InstallWorker(self._missing)
        self._inst.done.connect(self._install_done)
        self._inst.finished.connect(self._inst.deleteLater)
        self._inst.start()

    def _install_done(self, ok):
        self.log_line("Package install " + ("done." if ok else "FAILED."))
        self.refresh_toolbox()

    # ------------------------------------------------------------ queue
    def _add_paths(self, paths):
        for p in paths:
            try:
                probe = engine.probe_rom(str(p))
            except Exception as e:  # noqa: BLE001
                self.log_line(f"skip {p}: {e}")
                continue
            self.items.append(probe)
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._fill_row(row, probe,
                           "needs review" if probe["needs_review"] else "ready")
        self.table.resizeColumnsToContents()

    def _fill_row(self, row, probe, status):
        vals = [probe["title"], probe["mbc"],
                f"{probe['rom_size_kb']}K", f"{probe['ram_size_kb']}K",
                _cgb_label(probe), probe["region"] or "—", status]
        for c, val in enumerate(vals):
            self.table.setItem(row, c, QTableWidgetItem(val))

    def _set_status(self, row, status):
        self.table.setItem(row, STATUS_COL, QTableWidgetItem(status))

    def add_roms(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add Game Boy ROMs", "",
            "Game Boy ROMs (*.gb *.gbc *.GB *.GBC)")
        self._add_paths(paths)

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Add a folder of ROMs")
        if d:
            paths = []
            for pat in ("*.gb", "*.gbc", "*.GB", "*.GBC"):
                paths += Path(d).glob(pat)
            self._add_paths(sorted(paths))

    def remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self.table.removeRow(r)
            del self.items[r]

    def clear_queue(self):
        self.table.setRowCount(0)
        self.items.clear()

    def show_selected(self):
        rows = self.table.selectedIndexes()
        if not rows:
            return
        row = rows[0].row()
        if not 0 <= row < len(self.items):
            return
        probe = self.items[row]
        self.p_title.setText(probe["header_title"] or probe["title"])
        self.p_cart.setText(
            f"{probe['mbc']} (0x{probe['cart_type']:02X})"
            + (" , battery" if probe["battery"] else ""))
        self.p_sizes.setText(
            f"{probe['rom_size_kb']}K / {probe['ram_size_kb']}K")
        self.p_cgb.setText(_cgb_label(probe))
        self.p_region.setText(probe["region"] or "—")
        self.p_crc.setText(probe["crc32"])
        self.p_cover.setText("fetching art…")
        # One fetch per distinct cover, never a fresh thread per
        # selection change: scrolling fires this on every row, and
        # fetches take seconds when a thumbnail host is slow. The
        # wanted-key check in _cover_done discards stale results.
        key = (probe["title"], probe["cgb"], probe.get("sgb", False),
               probe.get("cgb_flag"))
        self._cover_wanted = key
        if any(w.key == key for w in self._cover_workers):
            return
        worker = CoverWorker(*key)
        worker.key = key
        self._cover_workers.add(worker)
        worker.done.connect(
            lambda t, p, k=key: self._cover_done(k, t, p))
        worker.finished.connect(
            lambda wk=worker: self._cover_finished(wk))
        worker.start()

    def _cover_finished(self, worker):
        self._cover_workers.discard(worker)
        worker.deleteLater()

    def _cover_done(self, key, title, path):
        if key != self._cover_wanted:
            return          # selection moved on; stale result
        rows = self.table.selectedIndexes()
        if not rows:
            return
        row = rows[0].row()
        if not 0 <= row < len(self.items):
            return
        if self.items[row]["title"] != title:
            return
        if path:
            pix = QPixmap(str(path))
            if not pix.isNull():
                self.p_cover.setPixmap(pix.scaled(
                    160, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                return
        self.p_cover.setText("(no art found)")

    # ------------------------------------------------------------ run
    def current_settings(self):
        return {
            "output_dir": self.s_out.text().strip(),
            "ra": self.s_ra.isChecked(),
            "ra_popups": self.s_popups.isChecked(),
            "ra_toml": self.s_toml.text().strip() or None,
            "targets": [t for t, on in [("linux", self.s_linux.isChecked()),
                                        ("windows", self.s_windows.isChecked())] if on],
            "jobs": self.s_jobs.value(),
        }

    def run_queue(self):
        if self.worker and self.worker.isRunning():
            return
        if not self.items:
            QMessageBox.information(self, "GBRecomp Tools",
                                    "Add at least one ROM first.")
            return
        s = self.current_settings()
        if not s["targets"]:
            QMessageBox.information(self, "GBRecomp Tools",
                                    "Pick at least one build target.")
            return
        self._save_settings(s)
        jobs = []
        for row, probe in enumerate(self.items):
            job = dict(s)
            job["rom"] = probe["rom"]
            job["title"] = probe["title"]
            jobs.append((row, job))
        self.run_btn.setEnabled(False)
        self.worker = Worker(jobs, s)
        self.worker.log.connect(self.log_line)
        self.worker.status.connect(self._set_status)
        self.worker.finished_all.connect(self._run_done)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()

    def _run_done(self):
        self.run_btn.setEnabled(True)
        self.log_line("Queue finished.")

    # ------------------------------------------------------------ settings
    def _load_settings(self):
        try:
            s = json.loads(SETTINGS_FILE.read_text())
        except Exception:  # noqa: BLE001
            return
        self.s_ra.setChecked(s.get("ra", True))
        self.s_popups.setChecked(s.get("ra_popups", True))
        self.s_toml.setText(s.get("ra_toml") or "")
        self.s_linux.setChecked("linux" in s.get("targets", ["linux", "windows"]))
        self.s_windows.setChecked("windows" in s.get("targets", ["linux", "windows"]))
        self.s_jobs.setValue(int(s.get("jobs", 2)))
        if s.get("output_dir"):
            self.s_out.setText(s["output_dir"])

    def _save_settings(self, s=None):
        try:
            SETTINGS_FILE.write_text(json.dumps(s or self.current_settings(),
                                                indent=2))
        except Exception:  # noqa: BLE001
            pass


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
