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
import shutil
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

APP_VERSION = "full45"


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
        out.append({"name": d.name, "project_dir": str(d), "config_file": str(yamls[0]), "base_config": infer_base_config_from_config(repo_root, yamls[0])})
    return out


def discover_base_configs(repo_root: Path) -> list[Path]:
    cfgdir = repo_root / "config"
    if not cfgdir.exists():
        return []
    return sorted([p for p in cfgdir.iterdir() if p.is_file() and p.suffix.lower() in (".yaml", ".yml")])


def iter_simple_includes(cfg_path: Path) -> list[Path]:
    out: list[Path] = []
    try:
        txt = cfg_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out

    # include: somefile.yaml
    for m in re.finditer(r"^\s*include\s*:\s*['\"]?([^'\"\n#]+\.ya?ml)['\"]?\s*$", txt, flags=re.M):
        p = Path(m.group(1).strip())
        if not p.is_absolute():
            p = (cfg_path.parent / p).resolve()
        out.append(p)

    # include:\n  file: something.yaml
    in_include_block = False
    include_indent = None
    for raw in txt.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^\s*include\s*:\s*$", raw):
            in_include_block = True
            include_indent = len(raw) - len(raw.lstrip())
            continue
        if in_include_block:
            cur_indent = len(raw) - len(raw.lstrip())
            if cur_indent <= (include_indent or 0):
                in_include_block = False
            else:
                m = re.match(r"^\s*file\s*:\s*['\"]?([^'\"\n#]+\.ya?ml)['\"]?\s*$", raw)
                if m:
                    p = Path(m.group(1).strip())
                    if not p.is_absolute():
                        p = (cfg_path.parent / p).resolve()
                    out.append(p)
    return out


def infer_base_config_from_config(repo_root: Path, cfg_path: Path, seen: set[str] | None = None) -> str:
    if seen is None:
        seen = set()
    try:
        rp = str(cfg_path.resolve())
    except Exception:
        rp = str(cfg_path)
    if rp in seen:
        return ""
    seen.add(rp)

    base_cfgs = {p.name: str(p.resolve()) for p in discover_base_configs(repo_root)}

    # direct name match first
    if cfg_path.name in base_cfgs:
        return base_cfgs[cfg_path.name]

    # if current config lives under repo_root/config and matches by filename stem hints
    if cfg_path.parent.resolve() == (repo_root / "config").resolve():
        return str(cfg_path.resolve())

    for inc in iter_simple_includes(cfg_path):
        if inc.name in base_cfgs:
            return base_cfgs[inc.name]
        found = infer_base_config_from_config(repo_root, inc, seen)
        if found:
            return found
    return ""




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
    ab_enabled: bool = False
    ab_slots: int = 2
    partition_model_enabled: bool = False

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
        self.tab_hooks = QWidget()
        self.tab_log = QWidget()

        self.tabs.addTab(self.tab_build, "Build")
        self.tabs.addTab(self.tab_profile, "Profile")
        self.tabs.addTab(self.tab_partitions, "Partitions")
        self.tabs.addTab(self.tab_chroot, "Chroot tasks")
        self.tabs.addTab(self.tab_examples, "Examples")
        self.tabs.addTab(self.tab_layers, "Layers")
        self.tabs.addTab(self.tab_hooks, "Hooks")
        self.tabs.addTab(self.tab_log, "Log")

        self.build_build_tab()
        self.build_profile_tab()
        self.build_partitions_tab()
        self.build_chroot_tab()
        self.build_examples_tab()
        self.build_layers_tab()
        self.build_hooks_tab()
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
        prof.layout = "ab" if self.is_ab_selected() else "rpios_single"
        prof.base_config = self.base_config.currentText().strip()
        prof.project_dir = self.project_dir.currentText().strip()
        prof.config_file = self.config_file.currentText().strip()
        prof.workroot = self.workroot.text().strip()
        prof.output_dir = self.output_dir.text().strip()
        prof.chroot_enabled = self.chk_chroot_enabled.isChecked()
        prof.ab_enabled = self.is_ab_selected()
        prof.ab_slots = int(self.ab_slots.currentText())
        prof.partition_model_enabled = True
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
                base_config=item.get("base_config", ""),
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
        self.base_config = QComboBox()
        self.base_config.setEditable(True)
        self.base_config.addItem("")
        for p in discover_base_configs(self.repo_root):
            self.base_config.addItem(str(p))
        self.base_config.currentTextChanged.connect(lambda _=None: self.sync_partition_preset_from_config())

        self.project_dir = QComboBox()
        self.project_dir.setEditable(True)
        self.config_file = QComboBox()
        self.config_file.setEditable(True)
        self.config_file.currentTextChanged.connect(lambda _=None: self.sync_partition_preset_from_config())

        self.workroot = QLineEdit(str((self.repo_root / "work").resolve()))
        self.output_dir = QLineEdit(str((self.repo_root / "work").resolve()))

        form.addRow("Name", self.profile_name)
        form.addRow("Target", self.target)
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

        self.chk_managed_project = QCheckBox("Use managed project copy in gui/projects/<profile>")
        self.chk_managed_project.setChecked(True)
        root.addWidget(self.chk_managed_project)

        btn_refresh = QPushButton("Refresh project/config dropdowns")
        btn_refresh.clicked.connect(self.refresh_project_config_dropdowns)
        root.addWidget(btn_refresh)

        self.ab_suggestion_label = QLabel("A/B suggestions: auto")
        self.ab_suggestion_label.setWordWrap(True)
        self.ab_suggestion_label.setStyleSheet("color:#666; font-size:11px;")
        root.addWidget(self.ab_suggestion_label)

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

        self.sync_partition_preset_from_config()
        self.update_ab_suggestions()

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
        self.sync_partition_preset_from_config()
        self.update_ab_suggestions()

    def load_profile_into_ui(self, prof: Profile):
        self.profile_name.setText(prof.name)
        self.target.setCurrentText(prof.target or "pi5")
        inferred_base = prof.base_config or (infer_base_config_from_config(self.repo_root, Path(prof.config_file)) if prof.config_file else "")
        self.base_config.setCurrentText(inferred_base)
        self.project_dir.setCurrentText(prof.project_dir or "")
        self.config_file.setCurrentText(prof.config_file or "")
        self.workroot.setText(prof.workroot or str((self.repo_root / "work").resolve()))
        self.output_dir.setText(prof.output_dir or str((self.repo_root / "work").resolve()))
        self.chk_chroot_enabled.setChecked(bool(prof.chroot_enabled))
        self.ab_slots.setCurrentText(str(getattr(prof, "ab_slots", 2)))
        self.overrides.setPlainText("\n".join(prof.overrides or []))
        self.extra_layers.setPlainText("\n".join(prof.extra_layers or []))

        self.part_table.setRowCount(0)
        if prof.partitions:
            for part in (prof.partitions or []):
                self.add_partition_row(part)
        else:
            self.sync_partition_preset_from_config()
        self.update_partition_summary()

        self.chroot_table.setRowCount(0)
        for t in (prof.chroot_tasks or []):
            self.add_chroot_row(t)

        self.update_ab_suggestions()

    # ---------------- Managed project integration ----------------

    def managed_project_dir(self, profile_name: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", profile_name.strip()) or "profile"
        return (self.projects_dir / safe).resolve()

    def ensure_managed_project(self, src_project_dir: Path, profile_name: str) -> Path:
        dst = self.managed_project_dir(profile_name)
        ensure_dir(dst)
        shutil.copytree(src_project_dir, dst, dirs_exist_ok=True)
        return dst

    def ensure_config_in_project(self, cfg_path: Path, project_dir: Path) -> Path:
        cfg_dir = project_dir / "config"
        ensure_dir(cfg_dir)
        dst = cfg_dir / cfg_path.name
        if cfg_path.resolve() != dst.resolve():
            shutil.copy2(cfg_path, dst)
        return dst


    def parse_simple_config_sizes(self, cfg_path: Path) -> dict:
        """Best-effort parse of size hints from YAML-like config files, following simple include/file chains."""
        return self.parse_config_sizes_with_includes(cfg_path, seen=set())

    def parse_config_sizes_with_includes(self, cfg_path: Path, seen: set[str]) -> dict:
        out = {}
        try:
            rp = str(cfg_path.resolve())
        except Exception:
            rp = str(cfg_path)
        if rp in seen:
            return out
        seen.add(rp)

        try:
            txt = cfg_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return out

        patterns = {
            "boot_part_size_mb": [
                r"^\s*boot_part_size\s*:\s*['\"]?(\d+)\s*[Mm]['\"]?\s*$",
                r"^\s*IGconf_gui_boot_part_size_mb\s*:\s*['\"]?(\d+)['\"]?\s*$",
            ],
            "root_part_size_mb": [
                r"^\s*root_part_size\s*:\s*['\"]?(\d+)\s*[Mm]['\"]?\s*$",
                r"^\s*IGconf_gui_root_part_size_mb\s*:\s*['\"]?(\d+)['\"]?\s*$",
            ],
            "rootfs_a_size_mb": [
                r"^\s*IGconf_gui_rootfs_a_size_mb\s*:\s*['\"]?(\d+)['\"]?\s*$",
            ],
            "rootfs_b_size_mb": [
                r"^\s*IGconf_gui_rootfs_b_size_mb\s*:\s*['\"]?(\d+)['\"]?\s*$",
            ],
            "data_part_size_mb": [
                r"^\s*IGconf_gui_data_part_size_mb\s*:\s*['\"]?(\d+)['\"]?\s*$",
            ],
        }
        for key, pats in patterns.items():
            for pat in pats:
                m = re.search(pat, txt, flags=re.M)
                if m:
                    out[key] = int(m.group(1))
                    break

        # Follow simple include chains:
        # include:
        #   file: something.yaml
        # or
        # include: something.yaml
        include_paths = []

        for m in re.finditer(r"^\s*include\s*:\s*['\"]?([^'\"\n#]+\.ya?ml)['\"]?\s*$", txt, flags=re.M):
            include_paths.append(m.group(1).strip())

        in_include_block = False
        include_indent = None
        for line in txt.splitlines():
            raw = line.rstrip("\n")
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if re.match(r"^\s*include\s*:\s*$", raw):
                in_include_block = True
                include_indent = len(raw) - len(raw.lstrip())
                continue
            if in_include_block:
                cur_indent = len(raw) - len(raw.lstrip())
                if cur_indent <= (include_indent or 0):
                    in_include_block = False
                else:
                    m = re.match(r"^\s*file\s*:\s*['\"]?([^'\"\n#]+\.ya?ml)['\"]?\s*$", raw)
                    if m:
                        include_paths.append(m.group(1).strip())

        for inc in include_paths:
            inc_path = Path(inc)
            if not inc_path.is_absolute():
                inc_path = (cfg_path.parent / inc_path).resolve()
            child = self.parse_config_sizes_with_includes(inc_path, seen)
            for k, v in child.items():
                if k not in out:
                    out[k] = v

        return out

    def apply_sizes_to_partition_table(self, sizes: dict):
        if not hasattr(self, "part_table"):
            return
        label_to_row = {}
        for r in range(self.part_table.rowCount()):
            item = self.part_table.item(r, 0)
            if item:
                label_to_row[item.text().strip().lower()] = r

        def set_size(label: str, key: str):
            if key not in sizes:
                return
            r = label_to_row.get(label.lower())
            if r is None:
                return
            self.part_table.setItem(r, 1, QTableWidgetItem(str(int(sizes[key]))))

        set_size("boot", "boot_part_size_mb")
        set_size("rootfs", "root_part_size_mb")
        set_size("rootfs_a", "rootfs_a_size_mb")
        set_size("rootfs_b", "rootfs_b_size_mb")
        set_size("data", "data_part_size_mb")
        self.update_partition_summary()

    def autodetect_partition_sizes_from_selection(self):
        """Read the selected config/base config and update partition sizes in the current preset."""
        if not hasattr(self, "part_table"):
            return
        selected = ""
        if hasattr(self, "base_config") and self.base_config.currentText().strip():
            selected = self.base_config.currentText().strip()
        elif hasattr(self, "config_file") and self.config_file.currentText().strip():
            selected = self.config_file.currentText().strip()
        if not selected:
            self.update_partition_summary()
            return
        p = Path(selected)
        if not p.exists():
            self.update_partition_summary()
            return
        sizes = self.parse_simple_config_sizes(p)
        if sizes:
            self.apply_sizes_to_partition_table(sizes)


    def infer_gui_config_metadata(self, prof: Profile) -> dict:
        model = self.partition_model_from_ui()
        parts = model.get("partitions", [])
        total_mb = sum(int(p.get("size_mb", 0) or 0) for p in parts)

        by_label = {}
        boot = None
        root = None
        for p in parts:
            label = (p.get("label") or "").lower()
            mp = (p.get("mountpoint") or "").strip()
            by_label[label] = p
            if boot is None and ("boot" in label or mp == "/boot"):
                boot = p
            if root is None and (mp == "/" or label == "rootfs"):
                root = p

        root_a = by_label.get("rootfs_a")
        root_b = by_label.get("rootfs_b")
        data = by_label.get("data")

        return {
            "profile": prof.name,
            "target": prof.target,
            "layout": prof.layout,
            "partition_model_enabled": True,
            "ab_enabled": bool(model.get("ab_enabled")),
            "ab_slots": int(model.get("ab_slots", 2) or 2),
            "partition_preset": model.get("preset", ""),
            "partition_count": len(parts),
            "partition_total_mb": total_mb,
            "partition_labels": ",".join((p.get("label") or "") for p in parts),
            "boot_part_size_mb": int((boot or {}).get("size_mb", 0) or 0),
            "root_part_size_mb": int((root or {}).get("size_mb", 0) or 0),
            "rootfs_a_size_mb": int((root_a or {}).get("size_mb", 0) or 0),
            "rootfs_b_size_mb": int((root_b or {}).get("size_mb", 0) or 0),
            "data_part_size_mb": int((data or {}).get("size_mb", 0) or 0),
        }

    def write_gui_managed_config(self, project_dir: Path, prof: Profile, source_cfg_path: Path) -> Path:
        cfg_dir = project_dir / "config"
        ensure_dir(cfg_dir)

        managed_path = cfg_dir / f"gui-managed-{source_cfg_path.stem}.yaml"
        meta = self.infer_gui_config_metadata(prof)

        lines = [
            "# Generated by rpi-image-gen GUI",
            "# This file is safe to regenerate. Edit the source config or GUI state instead.",
            "include:",
            f"  file: {source_cfg_path.name}",
            "",
            "env:",
            f"  IGconf_gui_profile: {meta['profile']}",
            f"  IGconf_gui_target: {meta['target']}",
            f"  IGconf_gui_layout: {meta['layout']}",
            f"  IGconf_gui_partition_model_enabled: {'1' if meta['partition_model_enabled'] else '0'}",
            f"  IGconf_gui_ab_enabled: {'1' if meta['ab_enabled'] else '0'}",
            f"  IGconf_gui_ab_slots: {meta['ab_slots']}",
            f"  IGconf_gui_partition_preset: {meta['partition_preset']}",
            f"  IGconf_gui_partition_count: {meta['partition_count']}",
            f"  IGconf_gui_partition_total_mb: {meta['partition_total_mb']}",
            f"  IGconf_gui_partition_labels: {meta['partition_labels'] or ''}",
        ]

        if meta["boot_part_size_mb"]:
            lines.append(f"  IGconf_gui_boot_part_size_mb: {meta['boot_part_size_mb']}")
        if meta["root_part_size_mb"]:
            lines.append(f"  IGconf_gui_root_part_size_mb: {meta['root_part_size_mb']}")
        if meta["rootfs_a_size_mb"]:
            lines.append(f"  IGconf_gui_rootfs_a_size_mb: {meta['rootfs_a_size_mb']}")
        if meta["rootfs_b_size_mb"]:
            lines.append(f"  IGconf_gui_rootfs_b_size_mb: {meta['rootfs_b_size_mb']}")
        if meta["data_part_size_mb"]:
            lines.append(f"  IGconf_gui_data_part_size_mb: {meta['data_part_size_mb']}")

        if meta["partition_model_enabled"] and (not meta["ab_enabled"]):
            image_lines = []
            if meta["boot_part_size_mb"]:
                image_lines.append(f"  boot_part_size: {meta['boot_part_size_mb']}M")
            if meta["root_part_size_mb"]:
                image_lines.append(f"  root_part_size: {meta['root_part_size_mb']}M")
            if image_lines:
                lines += ["", "image:"] + image_lines

        if meta["ab_enabled"]:
            lines += [
                "",
                "# GUI A/B preset metadata",
                "# These env values are intended for A/B-capable configs/layers to consume.",
                "# The GUI also suggests/auto-selects A/B-named configs and layers when available.",
            ]

        managed_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return managed_path

    def project_dir_for_build(self, prof: Profile) -> Path:
        pd_txt = (prof.project_dir or "").strip()
        if not pd_txt:
            return Path()
        src = Path(pd_txt).resolve()
        if not src.exists():
            return Path()
        if getattr(self, "chk_managed_project", None) and self.chk_managed_project.isChecked():
            return self.ensure_managed_project(src, prof.name)
        return src


    # Partitions tab
    def build_partitions_tab(self):
        l = QVBoxLayout(self.tab_partitions)
        hint = QLabel("Partition editor with GUI-managed partition model. A/B is derived automatically from the selected base config or config file.")
        hint.setWordWrap(True)
        l.addWidget(hint)

        ab_box = QGroupBox("Partition model")
        ab_form = QFormLayout(ab_box)
        self.ab_slots = QComboBox()
        self.ab_slots.addItems(["2"])
        self.ab_mode_label = QLabel("Derived mode: single-root")
        self.ab_mode_label.setStyleSheet("color:#666; font-size:11px;")
        ab_form.addRow("A/B mode", self.ab_mode_label)
        ab_form.addRow("Slots", self.ab_slots)
        l.addWidget(ab_box)

        self.partition_summary = QLabel("")
        self.partition_summary.setWordWrap(True)
        self.partition_summary.setStyleSheet("color:#666; font-size:11px;")
        l.addWidget(self.partition_summary)

        self.part_table = QTableWidget(0, 4)
        self.part_table.setHorizontalHeaderLabels(["Label", "Size (MB)", "FS", "Mountpoint"])
        self.part_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        l.addWidget(self.part_table, 1)

        btns = QHBoxLayout()
        b_add = QPushButton("Add")
        b_del = QPushButton("Remove selected")
        b_write = QPushButton("Write model now")
        b_single = QPushButton("Single-root preset")
        b_ab = QPushButton("A/B preset")
        b_add.clicked.connect(lambda: self.add_partition_row(Partition()))
        b_del.clicked.connect(self.remove_selected_partitions)
        b_write.clicked.connect(self.write_partition_model_now)
        b_single.clicked.connect(self.apply_single_partition_preset)
        b_ab.clicked.connect(self.apply_ab_partition_preset)
        btns.addWidget(b_add)
        btns.addWidget(b_del)
        btns.addWidget(b_write)
        btns.addWidget(b_single)
        btns.addWidget(b_ab)
        btns.addStretch(1)
        l.addLayout(btns)

        self.sync_partition_preset_from_config()

    def add_partition_row(self, part: Partition):
        r = self.part_table.rowCount()
        self.part_table.insertRow(r)
        self.part_table.setItem(r, 0, QTableWidgetItem(part.label))
        self.part_table.setItem(r, 1, QTableWidgetItem(str(part.size_mb)))
        self.part_table.setItem(r, 2, QTableWidgetItem(part.fs))
        self.part_table.setItem(r, 3, QTableWidgetItem(part.mountpoint))
        self.update_partition_summary()

    def remove_selected_partitions(self):
        rows = sorted({i.row() for i in self.part_table.selectedItems()}, reverse=True)
        for r in rows:
            self.part_table.removeRow(r)
        self.update_partition_summary()

    def is_ab_selected(self) -> bool:
        txt = (getattr(self, "base_config", None).currentText().strip() if getattr(self, "base_config", None) else "")
        cfg = (getattr(self, "config_file", None).currentText().strip() if getattr(self, "config_file", None) else "")
        # base config drives the mode when selected; otherwise fall back to config file
        cand = (txt or cfg).lower()
        name = Path(cand).name
        return name.endswith("-ab.yaml") or name.endswith("-ab.yml") or "-ab" in name or "_ab" in name

    def apply_single_partition_preset(self):
        self.part_table.setRowCount(0)
        self.add_partition_row(Partition(label="boot", size_mb=512, fs="vfat", mountpoint="/boot"))
        self.add_partition_row(Partition(label="rootfs", size_mb=4096, fs="ext4", mountpoint="/"))
        self.update_partition_summary()

    def apply_ab_partition_preset(self):
        self.part_table.setRowCount(0)
        self.add_partition_row(Partition(label="boot", size_mb=512, fs="vfat", mountpoint="/boot"))
        self.add_partition_row(Partition(label="rootfs_a", size_mb=3072, fs="ext4", mountpoint="/"))
        self.add_partition_row(Partition(label="rootfs_b", size_mb=3072, fs="ext4", mountpoint="/_b"))
        self.add_partition_row(Partition(label="data", size_mb=2048, fs="ext4", mountpoint="/data"))
        self.update_partition_summary()

    def sync_partition_preset_from_config(self):
        # build_profile_tab runs before build_partitions_tab, so these widgets may not exist yet
        if not hasattr(self, "part_table") or not hasattr(self, "ab_mode_label"):
            return
        current_labels = []
        for r in range(self.part_table.rowCount()):
            item = self.part_table.item(r, 0)
            current_labels.append((item.text() if item else "").lower())

        wants_ab = self.is_ab_selected()
        ab_labels = ["boot", "rootfs_a", "rootfs_b", "data"]
        single_labels = ["boot", "rootfs"]

        if wants_ab:
            self.ab_mode_label.setText("Derived mode: A/B")
            if current_labels != ab_labels:
                self.apply_ab_partition_preset()
            else:
                self.update_partition_summary()
        else:
            self.ab_mode_label.setText("Derived mode: single-root")
            if current_labels != single_labels:
                self.apply_single_partition_preset()
            else:
                self.update_partition_summary()
        self.autodetect_partition_sizes_from_selection()

    def update_partition_summary(self):
        if not hasattr(self, "part_table") or not hasattr(self, "partition_summary"):
            return
        parts = []
        total = 0
        for r in range(self.part_table.rowCount()):
            label = self.part_table.item(r, 0).text() if self.part_table.item(r, 0) else ""
            size = int(self.part_table.item(r, 1).text()) if self.part_table.item(r, 1) else 0
            total += size
            parts.append(f"{label}:{size}MB")
        preset = "A/B" if self.is_ab_selected() else "single-root"
        if hasattr(self, "ab_mode_label"):
            self.ab_mode_label.setText(f"Derived mode: {preset}")
        self.partition_summary.setText(f"{preset} preset, {len(parts)} partition(s), total {total}MB. " + ", ".join(parts) + " (sizes may be auto-detected from selected config)")

    def partition_model_from_ui(self) -> dict:
        parts = []
        for r in range(self.part_table.rowCount()):
            parts.append({
                "label": self.part_table.item(r, 0).text() if self.part_table.item(r, 0) else "",
                "size_mb": int(self.part_table.item(r, 1).text()) if self.part_table.item(r, 1) else 0,
                "fs": self.part_table.item(r, 2).text() if self.part_table.item(r, 2) else "",
                "mountpoint": self.part_table.item(r, 3).text() if self.part_table.item(r, 3) else "",
            })
        ab = self.is_ab_selected()
        return {
            "enabled": True,
            "ab_enabled": ab,
            "ab_slots": int(self.ab_slots.currentText()),
            "preset": "ab_dual_root" if ab else "single_root",
            "partitions": parts,
        }

    def write_partition_model_file(self, project_dir: Path, prof: Profile) -> Path:
        out_dir = project_dir / "gui"
        ensure_dir(out_dir)
        out = out_dir / "partition-model.json"
        model = self.partition_model_from_ui()
        model["profile"] = prof.name
        model["target"] = prof.target
        model["layout"] = prof.layout
        out.write_text(json.dumps(model, indent=2) + "\n", encoding="utf-8")
        return out

    def summarize_partition_model(self, prof: Profile) -> str:
        model = self.partition_model_from_ui()
        parts = model["partitions"]
        total = sum(int(p.get("size_mb", 0) or 0) for p in parts)
        names = ", ".join(p.get("label", "") for p in parts)
        ab = "enabled" if model["ab_enabled"] else "disabled"
        return f"partition-model: preset={model.get('preset','')}, {len(parts)} partition(s), total={total}MB, labels=[{names}], A/B={ab}"

    def write_partition_model_now(self):
        prof = self.current_profile_from_ui()
        proj_dir = self.project_dir_for_build(prof)
        if not proj_dir:
            QMessageBox.information(self, "Partition model", "Select a project dir first.")
            return
        try:
            pm = self.write_partition_model_file(Path(proj_dir), prof)
            self.log.appendPlainText(f"[gui] wrote partition model: {pm}")
            self.log.appendPlainText(f"[gui] {self.summarize_partition_model(prof)}")
            self.tabs.setCurrentWidget(self.tab_log)
        except Exception as e:
            QMessageBox.warning(self, "Partition model", f"Failed to write partition model: {e}")

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

        proj_dir = self.project_dir_for_build(prof)
        if proj_dir:
            args += ["-S", str(proj_dir)]
            try:
                pm = self.write_partition_model_file(Path(proj_dir), prof)
                self.log.appendPlainText(f"[gui] wrote partition model: {pm}")
                self.log.appendPlainText(f"[gui] {self.summarize_partition_model(prof)}")
                if self.is_ab_selected():
                    self.log.appendPlainText("[gui] A/B intent enabled. Select an A/B-capable config/layer for the actual image layout.")
            except Exception as e:
                QMessageBox.warning(self, "Partition model", f"Failed to write partition model: {e}")

        managed_cfg = None

        if prof.config_file:
            cfg_path = Path(prof.config_file).resolve()
            if proj_dir and cfg_path.exists():
                try:
                    cfg_path = self.ensure_config_in_project(cfg_path, proj_dir)
                    managed_cfg = self.write_gui_managed_config(Path(proj_dir), prof, cfg_path)
                    self.log.appendPlainText(f"[gui] wrote managed config: {managed_cfg}")
                except Exception as e:
                    QMessageBox.warning(self, "Managed config", f"Failed to prepare managed config: {e}")
            args += ["-c", str(managed_cfg or cfg_path)]
        elif prof.base_config:
            if proj_dir:
                try:
                    base_cfg = Path(prof.base_config).resolve()
                    managed_cfg = self.write_gui_managed_config(Path(proj_dir), prof, base_cfg)
                    self.log.appendPlainText(f"[gui] wrote managed config: {managed_cfg}")
                    args += ["-c", str(managed_cfg)]
                except Exception as e:
                    QMessageBox.warning(self, "Managed config", f"Failed to prepare managed config: {e}")
                    args += ["-c", prof.base_config]
            else:
                args += ["-c", prof.base_config]

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
        txt = f"Project: {ex['project_dir']}\nConfig: {ex['config_file']}\nBase config: {ex.get('base_config', '')}\n\n"
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
        prof = Profile(name=ex["name"], base_config=ex.get("base_config",""), project_dir=ex["project_dir"], config_file=ex["config_file"], overrides=[f"image.name={ex['name']}"])
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
        self.update_ab_suggestions()

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


    # ---------------- A/B suggestions ----------------

    def score_ab_candidate(self, name: str) -> int:
        n = (name or "").lower()
        score = 0
        for token, pts in [
            ("rota", 10),
            ("tryboot", 8),
            ("a-b", 8),
            ("_ab", 8),
            ("-ab", 8),
            ("ab-", 6),
            ("dual", 5),
            ("slot", 4),
            ("update", 3),
        ]:
            if token in n:
                score += pts
        return score

    def discover_ab_candidates(self) -> dict:
        config_candidates = []
        for combo in (getattr(self, "base_config", None), getattr(self, "config_file", None)):
            if not combo:
                continue
            for i in range(combo.count()):
                txt = combo.itemText(i).strip()
                if not txt:
                    continue
                score = self.score_ab_candidate(Path(txt).name)
                if score > 0:
                    config_candidates.append((score, txt))

        layer_candidates = []
        for name in getattr(self, "_all_layers", []) or []:
            score = self.score_ab_candidate(name)
            if score > 0:
                layer_candidates.append((score, name))

        config_candidates.sort(key=lambda x: (-x[0], x[1]))
        layer_candidates.sort(key=lambda x: (-x[0], x[1]))
        return {"configs": config_candidates, "layers": layer_candidates}

    def update_ab_suggestions(self):
        label = getattr(self, "ab_suggestion_label", None)
        if not label:
            return

        enabled = self.is_ab_selected()
        if not enabled:
            label.setText("A/B suggestions: choose a -ab base config or config file to enable A/B")
            return

        cand = self.discover_ab_candidates()
        cfgs = cand["configs"]
        layers = cand["layers"]

        cfg_txt = cfgs[0][1] if cfgs else "none found"
        layer_txt = ", ".join(x[1] for x in layers[:3]) if layers else "none found"

        label.setText(f"A/B active via config selection. Suggested layers={layer_txt}")

        # conservative auto-select: only if nothing is currently selected
        if cfgs:
            best = cfgs[0][1]
            if not self.config_file.currentText().strip() and not self.base_config.currentText().strip():
                # Prefer explicit config_file if the candidate is present there, else base_config
                set_done = False
                for i in range(self.config_file.count()):
                    if self.config_file.itemText(i).strip() == best:
                        self.config_file.setCurrentText(best)
                        set_done = True
                        break
                if not set_done:
                    for i in range(self.base_config.count()):
                        if self.base_config.itemText(i).strip() == best:
                            self.base_config.setCurrentText(best)
                            break

    def apply_ab_suggestions(self):
        QMessageBox.information(self, "A/B suggestions", "A/B is now driven automatically by the selected base config or config file.")

    # ---------------- Hooks tab ----------------

    def build_hooks_tab(self):
        l = QVBoxLayout(self.tab_hooks)

        top = QHBoxLayout()
        top.addWidget(QLabel("Project hooks (project_dir/bdebstrap/*)"), 1)
        self.btn_hooks_refresh = QPushButton("Refresh")
        self.btn_hooks_refresh.clicked.connect(self.refresh_hooks_list)
        self.btn_hooks_open_dir = QPushButton("Open hooks folder")
        self.btn_hooks_open_dir.clicked.connect(self.open_hooks_folder)
        top.addWidget(self.btn_hooks_refresh)
        top.addWidget(self.btn_hooks_open_dir)
        l.addLayout(top)

        mid = QSplitter(Qt.Horizontal)
        l.addWidget(mid, 1)

        left = QWidget()
        ll = QVBoxLayout(left)
        self.hooks_list = QListWidget()
        self.hooks_list.itemSelectionChanged.connect(self.on_hook_selected)
        ll.addWidget(self.hooks_list, 1)

        btns = QHBoxLayout()
        self.btn_hook_new = QPushButton("New hook…")
        self.btn_hook_new.clicked.connect(self.new_hook)
        self.btn_hook_save = QPushButton("Save")
        self.btn_hook_save.clicked.connect(self.save_hook)
        self.btn_hook_reload = QPushButton("Reload")
        self.btn_hook_reload.clicked.connect(self.reload_hook)
        btns.addWidget(self.btn_hook_new)
        btns.addWidget(self.btn_hook_save)
        btns.addWidget(self.btn_hook_reload)
        btns.addStretch(1)
        ll.addLayout(btns)

        mid.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        self.hook_path_label = QLabel("")
        self.hook_path_label.setStyleSheet("color:#666; font-size:11px;")
        rl.addWidget(self.hook_path_label)
        self.hook_editor = QPlainTextEdit()
        rl.addWidget(self.hook_editor, 1)
        mid.addWidget(right)
        mid.setSizes([320, 800])

        self._current_hook_path = None
        self.refresh_hooks_list()

    def hooks_dir_for_current_profile(self) -> Path | None:
        prof = self._profile_cache or self.current_profile_from_ui()
        proj_dir = self.project_dir_for_build(prof)
        if not proj_dir:
            return None
        hd = Path(proj_dir) / "bdebstrap"
        ensure_dir(hd)
        return hd

    def refresh_hooks_list(self):
        self.hooks_list.clear()
        self._current_hook_path = None
        self.hook_path_label.setText("")
        self.hook_editor.setPlainText("")

        hd = self.hooks_dir_for_current_profile()
        if not hd:
            return

        files = sorted([p for p in hd.iterdir() if p.is_file() and not p.name.startswith(".")])
        for p in files:
            it = QListWidgetItem(p.name)
            it.setData(Qt.UserRole, str(p))
            self.hooks_list.addItem(it)

    def on_hook_selected(self):
        it = self.hooks_list.currentItem()
        if not it:
            return
        p = Path(it.data(Qt.UserRole))
        self._current_hook_path = p
        self.hook_path_label.setText(str(p))
        try:
            self.hook_editor.setPlainText(p.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            QMessageBox.warning(self, "Hook", f"Failed to read {p}: {e}")

    def save_hook(self):
        if not self._current_hook_path:
            return
        try:
            self._current_hook_path.write_text(self.hook_editor.toPlainText(), encoding="utf-8")
            mode = self._current_hook_path.stat().st_mode
            self._current_hook_path.chmod(mode | 0o111)
            self.statusBar().showMessage(f"Saved hook: {self._current_hook_path.name}")
        except Exception as e:
            QMessageBox.warning(self, "Hook", f"Failed to save: {e}")

    def reload_hook(self):
        if self._current_hook_path and self._current_hook_path.exists():
            try:
                self.hook_editor.setPlainText(self._current_hook_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                pass

    def new_hook(self):
        hd = self.hooks_dir_for_current_profile()
        if not hd:
            QMessageBox.information(self, "Hooks", "Select a profile/project first.")
            return
        name, ok = QFileDialog.getSaveFileName(
            self,
            "New hook file",
            str(hd / "customize50-myhook.sh"),
            "Shell (*.sh);;All (*.*)"
        )
        if not ok or not name:
            return
        p = Path(name)
        if not p.exists():
            p.write_text("#!/bin/sh\nset -eu\n\n", encoding="utf-8")
            p.chmod(p.stat().st_mode | 0o111)
        self.refresh_hooks_list()
        for i in range(self.hooks_list.count()):
            it = self.hooks_list.item(i)
            if Path(it.data(Qt.UserRole)) == p:
                self.hooks_list.setCurrentItem(it)
                break

    def open_hooks_folder(self):
        hd = self.hooks_dir_for_current_profile()
        if not hd:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(hd.resolve())))


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
