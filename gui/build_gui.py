#!/usr/bin/env python3
# rpi-image-gen GUI (clean rebuild)
# Version: full28_clean

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QProcess, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QLineEdit,
    QComboBox,
    QPlainTextEdit,
    QMessageBox,
    QFileDialog,
    QCheckBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QFormLayout,
    QGroupBox,
    QStatusBar,
)

APP_VERSION = "full31_fixed"


def repo_root_from_script() -> Path:
    # this file is <repo>/gui/build_gui.py
    return Path(__file__).resolve().parent.parent


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def is_arm64() -> bool:
    m = platform.machine().lower()
    return m in ("aarch64", "arm64")


def default_cross_env() -> dict[str, str]:
    return {
        "ARCH": "arm64",
        "CROSS_COMPILE": os.environ.get("CROSS_COMPILE", "aarch64-linux-gnu-"),
    }


def discover_dirs(parent: Path) -> list[Path]:
    if not parent.exists():
        return []
    return sorted([p for p in parent.iterdir() if p.is_dir()])


def discover_examples(repo_root: Path) -> list[dict]:
    out: list[dict] = []
    ex_root = repo_root / "examples"
    if not ex_root.exists():
        return out
    for d in discover_dirs(ex_root):
        yamls = sorted(list(d.glob("*.yml")) + list(d.glob("*.yaml")))
        if not yamls:
            yamls = sorted(list((d / "config").glob("*.yml")) + list((d / "config").glob("*.yaml")))
        if not yamls:
            continue
        out.append({"name": d.name, "project_dir": str(d), "config_file": str(yamls[0])})
    return out


def discover_base_configs(repo_root: Path) -> list[Path]:
    cfgdir = repo_root / "config"
    if not cfgdir.exists():
        return []
    return sorted([p for p in cfgdir.iterdir() if p.is_file() and p.suffix.lower() in (".yaml", ".yml")])



@dataclass
class ChrootTask:
    name: str = "task"
    workdir: str = "/"
    script: str = "echo hello"


@dataclass
class Partition:
    label: str = "rootfs"
    size_mb: int = 2048
    fs: str = "ext4"
    mountpoint: str = "/"


@dataclass
class Profile:
    name: str = "profile"
    target: str = "pi5"
    layout: str = "rpios_single"
    base_config: str = ""
    project_dir: str = ""
    config_file: str = ""
    workroot: str = ""
    output_dir: str = ""
    overrides: list[str] = field(default_factory=list)
    extra_layers: list[str] = field(default_factory=list)
    partitions: list[Partition] = field(default_factory=list)
    chroot_tasks: list[ChrootTask] = field(default_factory=list)
    chroot_enabled: bool = False

    @staticmethod
    def from_dict(d: dict) -> "Profile":
        p = Profile()
        for k, v in (d or {}).items():
            if k == "partitions":
                p.partitions = [Partition(**x) for x in (v or [])]
            elif k == "chroot_tasks":
                p.chroot_tasks = [ChrootTask(**x) for x in (v or [])]
            else:
                if hasattr(p, k):
                    setattr(p, k, v)
        return p


class BuildGui(QMainWindow):
    def __init__(self):
        super().__init__()
        self.repo_root = repo_root_from_script()
        self.gui_root = (self.repo_root / "gui").resolve()
        self.profiles_dir = (self.gui_root / "profiles").resolve()
        self.projects_dir = (self.gui_root / "projects").resolve()
        ensure_dir(self.profiles_dir)
        ensure_dir(self.projects_dir)

        self.current_profile_path: Optional[Path] = None
        self._profile_cache: Optional[Profile] = None

        self.setWindowTitle(f"rpi-image-gen GUI ({APP_VERSION})")
        self.resize(1280, 800)
        self.setStatusBar(QStatusBar(self))

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        self.splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(self.splitter, 1)

        # Left: profiles
        left = QWidget()
        left.setMinimumWidth(320)
        left_l = QVBoxLayout(left)

        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("Profiles"))
        title_row.addStretch(1)
        self.btn_profiles_dir = QPushButton("Folder…")
        self.btn_profiles_dir.clicked.connect(self.pick_profiles_dir)
        title_row.addWidget(self.btn_profiles_dir)
        left_l.addLayout(title_row)

        self.profile_info = QLabel("")
        self.profile_info.setWordWrap(True)
        self.profile_info.setStyleSheet("color:#666; font-size:11px;")
        left_l.addWidget(self.profile_info)

        self.profile_list = QListWidget()
        self.profile_list.itemSelectionChanged.connect(self.on_profile_selected)
        left_l.addWidget(self.profile_list, 1)

        btns = QHBoxLayout()
        self.btn_new_profile = QPushButton("New")
        self.btn_save_profile = QPushButton("Save")
        self.btn_save_as_profile = QPushButton("Save as…")
        self.btn_delete_profile = QPushButton("Delete")
        self.btn_import_examples = QPushButton("Import examples → profiles")

        self.btn_new_profile.clicked.connect(self.new_profile)
        self.btn_save_profile.clicked.connect(self.save_profile)
        self.btn_save_as_profile.clicked.connect(self.save_profile_as)
        self.btn_delete_profile.clicked.connect(self.delete_profile)
        self.btn_import_examples.clicked.connect(self.import_examples)

        btns.addWidget(self.btn_new_profile)
        btns.addWidget(self.btn_save_profile)
        btns.addWidget(self.btn_save_as_profile)
        btns.addWidget(self.btn_delete_profile)
        left_l.addLayout(btns)
        left_l.addWidget(self.btn_import_examples)

        self.splitter.addWidget(left)

        # Right: tabs
        right = QWidget()
        right_l = QVBoxLayout(right)
        self.tabs = QTabWidget()
        right_l.addWidget(self.tabs, 1)
        self.splitter.addWidget(right)

        self.splitter.setSizes([360, 920])

        self.tab_build = QWidget()
        self.tab_profile = QWidget()
        self.tab_partitions = QWidget()
        self.tab_chroot = QWidget()
        self.tab_examples = QWidget()
        self.tab_layers = QWidget()
        self.tab_log = QWidget()

        self.tabs.addTab(self.tab_build, "Build")
        self.tabs.addTab(self.tab_profile, "Profile")
        self.tabs.addTab(self.tab_partitions, "Partitions")
        self.tabs.addTab(self.tab_chroot, "Chroot tasks")
        self.tabs.addTab(self.tab_examples, "Examples")
        self.tabs.addTab(self.tab_layers, "Layers")
        self.tabs.addTab(self.tab_log, "Log")

        self.build_build_tab()
        self.build_profile_tab()
        self.build_partitions_tab()
        self.build_chroot_tab()
        self.build_examples_tab()
        self.build_layers_tab()
        self.build_log_tab()

        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self.on_output)
        self.proc.finished.connect(self.on_finished)
        self._last_command: list[str] = []

        self.refresh_profile_list()
        self.statusBar().showMessage(f"Repo: {self.repo_root}")

    # Profiles
    def pick_profiles_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select profiles folder", str(self.profiles_dir))
        if not d:
            return
        self.profiles_dir = Path(d).resolve()
        ensure_dir(self.profiles_dir)
        self.refresh_profile_list()

    def refresh_profile_list(self):
        self.profile_list.blockSignals(True)
        self.profile_list.clear()

        pd = Path(self.profiles_dir)
        ensure_dir(pd)
        files = sorted([p for p in pd.iterdir() if p.is_file() and p.suffix.lower() == ".json"])
        for p in files:
            it = QListWidgetItem(p.stem)
            it.setData(Qt.UserRole, str(p))
            self.profile_list.addItem(it)

        self.profile_list.blockSignals(False)
        self.profile_info.setText(f"{len(files)} profile(s)\n{pd}")

    def on_profile_selected(self):
        it = self.profile_list.currentItem()
        if not it:
            return
        self.load_profile(Path(it.data(Qt.UserRole)))

    def load_profile(self, path: Path):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            prof = Profile.from_dict(d)
            prof.name = path.stem
            self.current_profile_path = path
            self._profile_cache = prof
            self.load_profile_into_ui(prof)
            self.statusBar().showMessage(f"Loaded: {path.name}")
        except Exception as e:
            QMessageBox.critical(self, "Failed to load profile", str(e))

    def new_profile(self):
        name, ok = QFileDialog.getSaveFileName(self, "New profile", str(self.profiles_dir / "new-profile.json"), "JSON (*.json)")
        if not ok or not name:
            return
        p = Path(name)
        if p.suffix.lower() != ".json":
            p = p.with_suffix(".json")
        prof = Profile(name=p.stem)
        p.write_text(json.dumps(asdict(prof), indent=2) + "\n", encoding="utf-8")
        self.refresh_profile_list()
        self.load_profile(p)

    def current_profile_from_ui(self) -> Profile:
        prof = self._profile_cache or Profile()
        prof.name = (self.profile_name.text() or "profile").strip()
        prof.target = self.target.currentText().strip()
        prof.layout = self.layout.currentText().strip()
        prof.base_config = self.base_config.currentText().strip()
        prof.project_dir = self.project_dir.currentText().strip()
        prof.config_file = self.config_file.currentText().strip()
        prof.workroot = self.workroot.text().strip()
        prof.output_dir = self.output_dir.text().strip()
        prof.chroot_enabled = self.chk_chroot_enabled.isChecked()
        prof.overrides = [ln.strip() for ln in self.overrides.toPlainText().splitlines() if ln.strip()]
        prof.extra_layers = [ln.strip() for ln in self.extra_layers.toPlainText().splitlines() if ln.strip()]

        prof.partitions = []
        for r in range(self.part_table.rowCount()):
            prof.partitions.append(Partition(
                label=self.part_table.item(r, 0).text() if self.part_table.item(r, 0) else "",
                size_mb=int(self.part_table.item(r, 1).text()) if self.part_table.item(r, 1) else 0,
                fs=self.part_table.item(r, 2).text() if self.part_table.item(r, 2) else "",
                mountpoint=self.part_table.item(r, 3).text() if self.part_table.item(r, 3) else "",
            ))

        prof.chroot_tasks = []
        for r in range(self.chroot_table.rowCount()):
            prof.chroot_tasks.append(ChrootTask(
                name=self.chroot_table.item(r, 0).text() if self.chroot_table.item(r, 0) else "",
                workdir=self.chroot_table.item(r, 1).text() if self.chroot_table.item(r, 1) else "/",
                script=self.chroot_table.item(r, 2).text() if self.chroot_table.item(r, 2) else "",
            ))
        return prof

    def save_profile(self):
        if not self.current_profile_path:
            return self.save_profile_as()
        prof = self.current_profile_from_ui()
        self.current_profile_path.write_text(json.dumps(asdict(prof), indent=2) + "\n", encoding="utf-8")
        self._profile_cache = prof
        self.refresh_profile_list()
        self.statusBar().showMessage(f"Saved: {self.current_profile_path.name}")

    def save_profile_as(self):
        name, ok = QFileDialog.getSaveFileName(self, "Save profile as", str(self.profiles_dir / f"{self.profile_name.text().strip() or 'profile'}.json"), "JSON (*.json)")
        if not ok or not name:
            return
        p = Path(name)
        if p.suffix.lower() != ".json":
            p = p.with_suffix(".json")
        self.current_profile_path = p
        self.save_profile()

    def delete_profile(self):
        it = self.profile_list.currentItem()
        if not it:
            return
        path = Path(it.data(Qt.UserRole))
        if QMessageBox.question(self, "Delete", f"Delete profile {path.name}?", QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            path.unlink()
        except Exception as e:
            QMessageBox.critical(self, "Failed", str(e))
            return
        self.current_profile_path = None
        self._profile_cache = None
        self.refresh_profile_list()

    def import_examples(self):
        ex = discover_examples(self.repo_root)
        created = 0
        skipped = 0
        for item in ex:
            out = self.profiles_dir / f"{item['name']}.json"
            if out.exists():
                skipped += 1
                continue
            prof = Profile(
                name=item["name"],
                target="pi5",
                layout="rpios_single",
                project_dir=item["project_dir"],
                config_file=item["config_file"],
                overrides=[f"image.name={item['name']}"],
            )
            out.write_text(json.dumps(asdict(prof), indent=2) + "\n", encoding="utf-8")
            created += 1
        self.refresh_profile_list()
        QMessageBox.information(self, "Imported", f"Created {created}, skipped {skipped}")

    # Profile tab
    def build_profile_tab(self):
        root = QVBoxLayout(self.tab_profile)
        form = QFormLayout()

        self.profile_name = QLineEdit()
        self.target = QComboBox()
        self.target.addItems(["pi5", "cm5"])
        self.layout = QComboBox()
        self.layout.addItems(["rpios_single", "ab (future)"])
        self.base_config = QComboBox()
        self.base_config.setEditable(True)
        self.base_config.addItem("")
        for p in discover_base_configs(self.repo_root):
            self.base_config.addItem(str(p))

        self.project_dir = QComboBox()
        self.project_dir.setEditable(True)
        self.config_file = QComboBox()
        self.config_file.setEditable(True)

        self.workroot = QLineEdit(str((self.repo_root / "work").resolve()))
        self.output_dir = QLineEdit(str((self.repo_root / "work").resolve()))

        form.addRow("Name", self.profile_name)
        form.addRow("Target", self.target)
        form.addRow("Layout", self.layout)
        form.addRow("Base config", self.base_config)

        pd_row = QHBoxLayout()
        pd_row.addWidget(self.project_dir, 1)
        btn_pd = QPushButton("Browse…")
        btn_pd.clicked.connect(self.browse_project_dir)
        pd_row.addWidget(btn_pd)
        form.addRow("Project dir (-S)", pd_row)

        cf_row = QHBoxLayout()
        cf_row.addWidget(self.config_file, 1)
        btn_cf = QPushButton("Browse…")
        btn_cf.clicked.connect(self.browse_config_file)
        cf_row.addWidget(btn_cf)
        form.addRow("Config file (-c)", cf_row)

        form.addRow("Work root", self.workroot)
        form.addRow("Output dir", self.output_dir)
        root.addLayout(form)

        row = QHBoxLayout()
        self.overrides = QPlainTextEdit()
        self.overrides.setPlaceholderText("Overrides (one per line), e.g. image.name=myimg")
        self.extra_layers = QPlainTextEdit()
        self.extra_layers.setPlaceholderText("Extra layers (one per line)")
        row.addWidget(self.overrides, 1)
        row.addWidget(self.extra_layers, 1)
        root.addLayout(row, 1)

        self.chk_chroot_enabled = QCheckBox("Enable chroot tasks after build")
        root.addWidget(self.chk_chroot_enabled)

        btn_refresh = QPushButton("Refresh project/config dropdowns")
        btn_refresh.clicked.connect(self.refresh_project_config_dropdowns)
        root.addWidget(btn_refresh)

        self.refresh_project_config_dropdowns()

    def refresh_project_config_dropdowns(self):
        cur_bc = self.base_config.currentText()
        self.base_config.blockSignals(True)
        self.base_config.clear()
        self.base_config.addItem("")
        for p in discover_base_configs(self.repo_root):
            self.base_config.addItem(str(p))
        if cur_bc:
            self.base_config.setCurrentText(cur_bc)
        self.base_config.blockSignals(False)

        cur_pd = self.project_dir.currentText()
        self.project_dir.blockSignals(True)
        self.project_dir.clear()
        self.project_dir.addItem("")
        for d in discover_dirs(self.projects_dir):
            self.project_dir.addItem(str(d))
        for e in discover_examples(self.repo_root):
            self.project_dir.addItem(e["project_dir"])
        if cur_pd:
            self.project_dir.setCurrentText(cur_pd)
        self.project_dir.blockSignals(False)

        cur_cf = self.config_file.currentText()
        self.config_file.blockSignals(True)
        self.config_file.clear()
        self.config_file.addItem("")
        pd_txt = self.project_dir.currentText().strip()
        if pd_txt:
            pd = Path(pd_txt)
            if pd.exists():
                yamls = sorted(list(pd.glob("*.yml")) + list(pd.glob("*.yaml")) + list((pd / "config").glob("*.yml")) + list((pd / "config").glob("*.yaml")))
                for y in yamls:
                    self.config_file.addItem(str(y))
        if cur_cf:
            self.config_file.setCurrentText(cur_cf)
        self.config_file.blockSignals(False)

    def browse_project_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select project dir", str(self.projects_dir))
        if not d:
            return
        self.project_dir.setCurrentText(str(Path(d).resolve()))
        self.refresh_project_config_dropdowns()

    def browse_config_file(self):
        f, ok = QFileDialog.getOpenFileName(self, "Select config file", str(self.repo_root), "YAML (*.yaml *.yml);;All (*.*)")
        if not ok or not f:
            return
        self.config_file.setCurrentText(str(Path(f).resolve()))

    def load_profile_into_ui(self, prof: Profile):
        self.profile_name.setText(prof.name)
        self.target.setCurrentText(prof.target or "pi5")
        self.layout.setCurrentText(prof.layout or "rpios_single")
        self.base_config.setCurrentText(prof.base_config or "")
        self.project_dir.setCurrentText(prof.project_dir or "")
        self.config_file.setCurrentText(prof.config_file or "")
        self.workroot.setText(prof.workroot or str((self.repo_root / "work").resolve()))
        self.output_dir.setText(prof.output_dir or str((self.repo_root / "work").resolve()))
        self.chk_chroot_enabled.setChecked(bool(prof.chroot_enabled))
        self.overrides.setPlainText("\n".join(prof.overrides or []))
        self.extra_layers.setPlainText("\n".join(prof.extra_layers or []))

        self.part_table.setRowCount(0)
        for part in (prof.partitions or []):
            self.add_partition_row(part)

        self.chroot_table.setRowCount(0)
        for t in (prof.chroot_tasks or []):
            self.add_chroot_row(t)

    # Partitions tab
    def build_partitions_tab(self):
        l = QVBoxLayout(self.tab_partitions)
        hint = QLabel("Partition editor (basic).")
        hint.setWordWrap(True)
        l.addWidget(hint)

        self.part_table = QTableWidget(0, 4)
        self.part_table.setHorizontalHeaderLabels(["Label", "Size (MB)", "FS", "Mountpoint"])
        self.part_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        l.addWidget(self.part_table, 1)

        btns = QHBoxLayout()
        b_add = QPushButton("Add")
        b_del = QPushButton("Remove selected")
        b_add.clicked.connect(lambda: self.add_partition_row(Partition()))
        b_del.clicked.connect(self.remove_selected_partitions)
        btns.addWidget(b_add)
        btns.addWidget(b_del)
        btns.addStretch(1)
        l.addLayout(btns)

        self.add_partition_row(Partition(label="boot", size_mb=512, fs="vfat", mountpoint="/boot"))
        self.add_partition_row(Partition(label="rootfs", size_mb=4096, fs="ext4", mountpoint="/"))

    def add_partition_row(self, part: Partition):
        r = self.part_table.rowCount()
        self.part_table.insertRow(r)
        self.part_table.setItem(r, 0, QTableWidgetItem(part.label))
        self.part_table.setItem(r, 1, QTableWidgetItem(str(part.size_mb)))
        self.part_table.setItem(r, 2, QTableWidgetItem(part.fs))
        self.part_table.setItem(r, 3, QTableWidgetItem(part.mountpoint))

    def remove_selected_partitions(self):
        rows = sorted({i.row() for i in self.part_table.selectedItems()}, reverse=True)
        for r in rows:
            self.part_table.removeRow(r)

    # Chroot tab
    def build_chroot_tab(self):
        l = QVBoxLayout(self.tab_chroot)
        hint = QLabel("Chroot tasks to run inside the image after build (best-effort).")
        hint.setWordWrap(True)
        l.addWidget(hint)

        self.chroot_table = QTableWidget(0, 3)
        self.chroot_table.setHorizontalHeaderLabels(["Name", "Workdir", "Script"])
        self.chroot_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        l.addWidget(self.chroot_table, 1)

        btns = QHBoxLayout()
        b_add = QPushButton("Add task")
        b_del = QPushButton("Remove selected")
        b_add.clicked.connect(lambda: self.add_chroot_row(ChrootTask()))
        b_del.clicked.connect(self.remove_selected_chroot)
        btns.addWidget(b_add)
        btns.addWidget(b_del)
        btns.addStretch(1)
        l.addLayout(btns)

        self.add_chroot_row(ChrootTask(name="clone+build", workdir="/", script="git clone https://example.com/repo && cd repo && make && make install"))

    def add_chroot_row(self, task: ChrootTask):
        r = self.chroot_table.rowCount()
        self.chroot_table.insertRow(r)
        self.chroot_table.setItem(r, 0, QTableWidgetItem(task.name))
        self.chroot_table.setItem(r, 1, QTableWidgetItem(task.workdir))
        self.chroot_table.setItem(r, 2, QTableWidgetItem(task.script))

    def remove_selected_chroot(self):
        rows = sorted({i.row() for i in self.chroot_table.selectedItems()}, reverse=True)
        for r in rows:
            self.chroot_table.removeRow(r)

    # Build tab
    def build_build_tab(self):
        l = QVBoxLayout(self.tab_build)

        box = QGroupBox("Build")
        form = QFormLayout(box)
        self.build_mode = QComboBox()
        self.build_mode.addItems(["build", "clean"])
        self.chk_cross = QCheckBox("Auto-set cross-compile env if not arm64")
        self.chk_cross.setChecked(True)
        self.chk_open_output = QCheckBox("Open output folder after build")
        self.chk_open_output.setChecked(True)
        form.addRow("Mode", self.build_mode)
        form.addRow("", self.chk_cross)
        form.addRow("", self.chk_open_output)
        l.addWidget(box)

        btns = QHBoxLayout()
        self.btn_run = QPushButton("Run")
        self.btn_stop = QPushButton("Stop")
        self.btn_copy = QPushButton("Copy command")
        self.btn_open_out = QPushButton("Open output folder")
        self.btn_resolve_cfg = QPushButton("Resolve config")
        self.btn_stop.setEnabled(False)

        self.btn_run.clicked.connect(self.run_build)
        self.btn_stop.clicked.connect(self.stop_command)
        self.btn_copy.clicked.connect(self.copy_command)
        self.btn_open_out.clicked.connect(self.open_best_output)
        self.btn_resolve_cfg.clicked.connect(self.resolve_config)

        btns.addWidget(self.btn_run)
        btns.addWidget(self.btn_stop)
        btns.addWidget(self.btn_copy)
        btns.addWidget(self.btn_open_out)
        btns.addWidget(self.btn_resolve_cfg)
        btns.addStretch(1)
        l.addLayout(btns)

        l.addWidget(QLabel("Output:"))
        self.build_log = QPlainTextEdit()
        self.build_log.setReadOnly(True)
        l.addWidget(self.build_log, 1)

    def rpig_script_path(self) -> Path:
        cand = self.repo_root / "rpi-image-gen"
        if cand.exists():
            return cand
        return cand

    def run_build(self):
        prof = self.current_profile_from_ui()
        rpig = self.rpig_script_path()
        if not rpig.exists():
            QMessageBox.warning(self, "Missing", f"Could not find rpi-image-gen script at {rpig}")
            return

        mode = self.build_mode.currentText().strip()
        args = [str(rpig), mode]
        if prof.project_dir:
            args += ["-S", prof.project_dir]
        if prof.config_file:
            args += ["-c", prof.config_file]
        if prof.base_config:
            args += ["-b", prof.base_config]
        for o in prof.overrides:
            if "=" in o:
                args += ["-O", o]

        self.run_command(args, cwd=str(self.repo_root), cross=(self.chk_cross.isChecked() and not is_arm64()))

    # Process runner
    def run_command(self, args: list[str], cwd: str, cross: bool = False):
        if self.proc.state() != QProcess.NotRunning:
            QMessageBox.warning(self, "Busy", "A command is already running.")
            return
        self._last_command = [str(a) for a in args]
        env = os.environ.copy()
        if cross:
            env.update(default_cross_env())

        self.proc.setWorkingDirectory(cwd)
        self.proc.setEnvironment([f"{k}={v}" for k, v in env.items()])

        self.build_log.appendPlainText("Running: " + " ".join(shlex.quote(a) for a in args))
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.proc.start(args[0], args[1:])

    def on_output(self):
        data = self.proc.readAllStandardOutput().data().decode(errors="replace")
        if data:
            self.build_log.appendPlainText(data.rstrip("\n"))
            self.log.appendPlainText(data.rstrip("\n"))

    def on_finished(self, exit_code: int, _exit_status):
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.build_log.appendPlainText(f"Finished (exit={exit_code})")
        self.statusBar().showMessage(f"Finished (exit={exit_code})")
        if self.chk_open_output.isChecked():
            self.open_best_output()


    def resolve_config(self):
        prof = self.current_profile_from_ui()
        rpig = self.rpig_script_path()
        cfg = (prof.config_file or prof.base_config or "").strip()
        if not cfg:
            QMessageBox.information(self, "Resolve config", "Select a config file or base config first.")
            return
        if not rpig.exists():
            QMessageBox.warning(self, "Missing", f"Could not find rpi-image-gen script at {rpig}")
            return

        tmp = Path(tempfile.gettempdir()) / "rpi-image-gen-gui.resolved.env"
        args = [str(rpig), "config", str(Path(cfg).resolve()), "--write-to", str(tmp)]

        p = QProcess(self)
        p.setWorkingDirectory(str(self.repo_root))
        p.setProcessChannelMode(QProcess.MergedChannels)
        if self.chk_cross.isChecked() and not is_arm64():
            env = os.environ.copy()
            env.update(default_cross_env())
            p.setEnvironment([f"{k}={v}" for k, v in env.items()])
        p.start(args[0], args[1:])
        p.waitForFinished(60_000)

        out = p.readAllStandardOutput().data().decode(errors="replace")
        self.log.appendPlainText("[gui] resolve-config command:")
        self.log.appendPlainText("  " + " ".join(shlex.quote(a) for a in args))
        if out.strip():
            self.log.appendPlainText(out.rstrip("\n"))
        if tmp.exists():
            self.log.appendPlainText("[gui] resolved env file:")
            self.log.appendPlainText(tmp.read_text(encoding="utf-8", errors="replace"))
        self.tabs.setCurrentWidget(self.tab_log)

    def stop_command(self):
        if self.proc.state() != QProcess.NotRunning:
            self.proc.kill()

    def copy_command(self):
        if not self._last_command:
            return
        QApplication.clipboard().setText(" ".join(shlex.quote(a) for a in self._last_command))
        self.statusBar().showMessage("Command copied")

    def open_best_output(self):
        prof = self.current_profile_from_ui()
        p = Path(prof.output_dir) if prof.output_dir else (self.repo_root / "work")
        if p.is_file():
            p = p.parent
        if not p.exists():
            p = p.parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.resolve())))

    # Examples tab
    def build_examples_tab(self):
        l = QVBoxLayout(self.tab_examples)
        l.addWidget(QLabel("Examples discovered in repo/examples (best-effort)."))
        self.examples_list = QListWidget()
        l.addWidget(self.examples_list, 1)

        btns = QHBoxLayout()
        b_refresh = QPushButton("Refresh")
        b_open = QPushButton("Open selected")
        b_profile = QPushButton("Create profile from selected")
        b_refresh.clicked.connect(self.refresh_examples_list)
        b_open.clicked.connect(self.open_selected_example)
        b_profile.clicked.connect(self.profile_from_selected_example)
        btns.addWidget(b_refresh)
        btns.addWidget(b_open)
        btns.addWidget(b_profile)
        btns.addStretch(1)
        l.addLayout(btns)

        self.examples_info = QPlainTextEdit()
        self.examples_info.setReadOnly(True)
        l.addWidget(self.examples_info, 1)

        self.examples_list.itemSelectionChanged.connect(self.on_example_selected)
        self.refresh_examples_list()

    def refresh_examples_list(self):
        self.examples_list.clear()
        self._examples = discover_examples(self.repo_root)
        for ex in self._examples:
            it = QListWidgetItem(ex["name"])
            it.setData(Qt.UserRole, ex)
            self.examples_list.addItem(it)

    def on_example_selected(self):
        it = self.examples_list.currentItem()
        if not it:
            return
        ex = it.data(Qt.UserRole)
        cfg = Path(ex["config_file"])
        txt = f"Project: {ex['project_dir']}\nConfig: {ex['config_file']}\n\n"
        try:
            txt += cfg.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
        self.examples_info.setPlainText(txt)

    def open_selected_example(self):
        it = self.examples_list.currentItem()
        if not it:
            return
        ex = it.data(Qt.UserRole)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(ex["project_dir"]).resolve())))

    def profile_from_selected_example(self):
        it = self.examples_list.currentItem()
        if not it:
            return
        ex = it.data(Qt.UserRole)
        out = self.profiles_dir / f"{ex['name']}.json"
        if out.exists():
            QMessageBox.information(self, "Exists", "Profile already exists.")
            return
        prof = Profile(name=ex["name"], project_dir=ex["project_dir"], config_file=ex["config_file"], overrides=[f"image.name={ex['name']}"])
        out.write_text(json.dumps(asdict(prof), indent=2) + "\n", encoding="utf-8")
        self.refresh_profile_list()


    # Layers tab
    def build_layers_tab(self):
        l = QVBoxLayout(self.tab_layers)

        row = QHBoxLayout()
        self.btn_layers_refresh = QPushButton("Refresh layers")
        self.btn_layers_refresh.clicked.connect(self.refresh_layers)
        self.btn_layer_describe = QPushButton("Describe selected")
        self.btn_layer_describe.clicked.connect(self.describe_selected_layer)
        self.layer_filter = QLineEdit()
        self.layer_filter.setPlaceholderText("Filter…")
        self.layer_filter.textChanged.connect(self.apply_layer_filter)

        row.addWidget(self.btn_layers_refresh, 2)
        row.addWidget(self.btn_layer_describe, 1)
        row.addWidget(self.layer_filter, 2)
        l.addLayout(row)

        self.layers_list = QListWidget()
        l.addWidget(self.layers_list, 1)

        self.layer_details = QPlainTextEdit()
        self.layer_details.setReadOnly(True)
        l.addWidget(self.layer_details, 1)

        self.layers_list.itemSelectionChanged.connect(self.describe_selected_layer)
        self._all_layers = []
        self.refresh_layers()

    def refresh_layers(self):
        rpig = self.rpig_script_path()
        if not rpig.exists():
            self.layer_details.setPlainText(f"Missing rpi-image-gen at {rpig}")
            return

        p = QProcess(self)
        p.setWorkingDirectory(str(self.repo_root))
        p.setProcessChannelMode(QProcess.MergedChannels)
        if self.chk_cross.isChecked() and not is_arm64():
            env = os.environ.copy()
            env.update(default_cross_env())
            p.setEnvironment([f"{k}={v}" for k, v in env.items()])
        p.start(str(rpig), ["layer", "--list"])
        p.waitForFinished(60_000)
        out = p.readAllStandardOutput().data().decode(errors="replace")

        layers = []
        for ln in out.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            layers.append(ln.split()[0])

        seen = set()
        self._all_layers = []
        for x in layers:
            if x not in seen:
                seen.add(x)
                self._all_layers.append(x)

        self.apply_layer_filter()
        self.layer_details.setPlainText(out.strip() or "(no output)")

    def apply_layer_filter(self):
        f = (self.layer_filter.text() or "").strip().lower()
        self.layers_list.clear()
        for name in self._all_layers:
            if not f or f in name.lower():
                self.layers_list.addItem(name)

    def describe_selected_layer(self):
        it = self.layers_list.currentItem()
        if not it:
            return
        name = it.text().strip()
        rpig = self.rpig_script_path()
        if not rpig.exists():
            return

        p = QProcess(self)
        p.setWorkingDirectory(str(self.repo_root))
        p.setProcessChannelMode(QProcess.MergedChannels)
        if self.chk_cross.isChecked() and not is_arm64():
            env = os.environ.copy()
            env.update(default_cross_env())
            p.setEnvironment([f"{k}={v}" for k, v in env.items()])
        p.start(str(rpig), ["layer", "--describe", name])
        p.waitForFinished(60_000)
        out = p.readAllStandardOutput().data().decode(errors="replace")
        self.layer_details.setPlainText(out.strip() or "(no output)")

    # Log tab
    def build_log_tab(self):
        l = QVBoxLayout(self.tab_log)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        l.addWidget(self.log, 1)
        self.log.appendPlainText(f"Started {APP_VERSION}")
        self.log.appendPlainText(f"Repo root: {self.repo_root}")
        self.log.appendPlainText(f"Profiles dir: {self.profiles_dir}")
        self.log.appendPlainText(f"Projects dir: {self.projects_dir}")


def main():
    app = QApplication(sys.argv)
    w = BuildGui()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
