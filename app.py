# -*- coding: utf-8 -*-
"""照片 / 视频智能分类整理程序  ——  拖进来，自动分类，自动编号。

全程离线运行：不上传任何文件，不调用任何云端接口，不消耗任何积分。
"""

from __future__ import annotations

import json
import os
import sys
import subprocess
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt, QThread, Signal, QSize, QTimer
from PySide6.QtGui import QFont, QColor, QIcon, QPixmap, QPainter, QBrush
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QCheckBox, QSlider, QFrame, QTableWidget,
    QTableWidgetItem, QProgressBar, QFileDialog, QMessageBox, QDialog,
    QListWidget, QComboBox, QPlainTextEdit, QAbstractItemView, QHeaderView,
    QInputDialog, QSizePolicy, QMenu, QDialogButtonBox,
)

from core.config import CategoryBook, Category, Settings, ALL_EXTS
from core.classifier import Classifier, SemanticEngine
from core.organizer import (Organizer, scan_folder, list_undo_files, undo,
                            undo_upto, _WARN_ROOTS)
from core.renamer import InPlaceRenamer, list_disk_undo_files, undo_disk, INPLACE_KIND

APP_TITLE = "照片 / 视频 智能分类整理"

BG = "#f4f6f9"
CARD = "#ffffff"
LINE = "#dfe3ea"
TEXT = "#2c3244"
MUTED = "#828b9e"
ACCENT = "#2f6fed"
OKC = "#17935a"
WARNC = "#d3820f"
ERRC = "#d64545"

QSS = f"""
QWidget {{ font-family: "Microsoft YaHei UI","Segoe UI",sans-serif; font-size: 13px; color: {TEXT}; }}
QMainWindow, #root {{ background: {BG}; }}
#drop {{ background: {CARD}; border: 2px dashed #c2cadb; border-radius: 10px; }}
#dropHot {{ background: #eaf1ff; border: 2px dashed {ACCENT}; border-radius: 10px; }}
#dropTitle {{ font-size: 19px; font-weight: 600; color: #39415a; }}
#dropSub {{ color: {MUTED}; font-size: 12px; }}
#card {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 8px; }}
QLineEdit {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 5px; padding: 5px 8px; }}
QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
QPushButton {{ background: #eaedf3; border: none; border-radius: 6px; padding: 7px 14px; }}
QPushButton:hover {{ background: #dfe4ec; }}
QPushButton:disabled {{ color: #aeb5c4; background: #f0f2f6; }}
QPushButton#primary {{ background: {ACCENT}; color: white; font-weight: 600; padding: 9px 20px; }}
QPushButton#primary:hover {{ background: #2559c4; }}
QPushButton#primary:disabled {{ background: #b9c8ea; color: #eef2fb; }}
QPushButton#go {{ background: {OKC}; color: white; font-weight: 600; padding: 9px 20px; }}
QPushButton#go:hover {{ background: #12784a; }}
QPushButton#go:disabled {{ background: #b6d8c7; color: #eaf5ef; }}
QPushButton#danger {{ background: #fbeaea; color: {ERRC}; }}
QPushButton#danger:hover {{ background: #f6dcdc; }}
QTableWidget {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 8px;
                gridline-color: #eef1f6; selection-background-color: #e6efff;
                selection-color: {TEXT}; }}
QHeaderView::section {{ background: #eef1f6; border: none; border-right: 1px solid #e3e7ef;
                        padding: 7px; font-weight: 600; }}
QProgressBar {{ background: #e6e9f0; border: none; border-radius: 5px; height: 10px; text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
QSlider::groove:horizontal {{ height: 4px; background: #dde2ea; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {ACCENT}; width: 14px; margin: -5px 0; border-radius: 7px; }}
QCheckBox {{ spacing: 6px; }}
QPlainTextEdit, QListWidget {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 6px; }}
QComboBox {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 5px; padding: 5px 8px; }}
"""


def _risky_root_reason(path: str) -> str:
    """判断一个扫描根目录值不值得先警告用户。

    返回空串 = 没问题；返回非空 = 需要二次确认的理由。
    整盘扫描（D:\\）和直接扫系统目录是两类典型的高风险操作。
    """
    ap = os.path.abspath(path)
    parts = [p for p in ap.replace("/", os.sep).split(os.sep) if p]
    for p in parts:
        if p.lower() in {x.lower() for x in _WARN_ROOTS}:
            return (f"「{ap}」位于系统目录 {p} 之下。\n"
                    f"这类目录里有几十万个小文件，扫描会非常慢，还常常报权限错误。")
    # 盘符根目录：D:\ 这种，长度只剩一个盘符
    if len(parts) <= 1 and len(ap) <= 3:
        return (f"「{ap}」是整个磁盘根目录。\n"
                f"会递归扫描盘上所有文件夹，通常要很久；建议直接选放照片的那个文件夹。")
    return ""


def icon_dot(color: str) -> QIcon:
    pm = QPixmap(12, 12)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QBrush(QColor(color)))
    p.setPen(Qt.NoPen)
    p.drawEllipse(1, 1, 10, 10)
    p.end()
    return QIcon(pm)


# =========================================================================== #
class ScanWorker(QThread):
    status = Signal(str)
    total = Signal(int)
    row = Signal(int, int, object)
    done = Signal(list, str)
    failed = Signal(str)

    def __init__(self, book, settings, folder):
        super().__init__()
        self.book, self.settings, self.folder = book, settings, folder
        self.organizer: Organizer | None = None

    def run(self):
        try:
            clf = Classifier(self.book, use_ai=self.settings.use_ai)
            ok, msg = clf.prepare(self.status.emit)
            self.status.emit(msg)
            self.organizer = Organizer(self.book, self.settings, clf)
            files = scan_folder(self.folder, self.book, self.settings.recursive)
            if not files:
                self.done.emit([], "没找到可整理的媒体文件（已分好类的类别文件夹会自动跳过）")
                return
            self.total.emit(len(files))
            items = self.organizer.build_plan(
                self.folder, files, lambda i, t, p, it: self.row.emit(i, t, it)
            )
            self.organizer.assign_names(self.folder, items)
            self.done.emit(items, "")
        except Exception:
            self.failed.emit(traceback.format_exc())


class ExecWorker(QThread):
    row = Signal(int, int, object)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, organizer, folder, items):
        super().__init__()
        self.organizer, self.folder, self.items = organizer, folder, items

    def run(self):
        try:
            rep = self.organizer.execute(
                self.folder, self.items, lambda i, t, p, it: self.row.emit(i, t, it)
            )
            self.done.emit(rep)
        except Exception:
            self.failed.emit(traceback.format_exc())


# =========================================================================== #
class DiskScanWorker(QThread):
    status = Signal(str)
    total = Signal(int)
    row = Signal(int, int, object)
    done = Signal(list, str)
    failed = Signal(str)

    def __init__(self, book, settings, roots):
        super().__init__()
        self.book, self.settings, self.roots = book, settings, roots
        self.renamer: InPlaceRenamer | None = None

    def run(self):
        try:
            clf = Classifier(self.book, use_ai=self.settings.use_ai)
            ok, msg = clf.prepare(self.status.emit)
            self.status.emit(msg)
            ren = InPlaceRenamer(self.book, self.settings, clf)
            self.renamer = ren
            items = ren.build_plan(self.roots, lambda i, t, p, it: self.row.emit(i, t, it))
            ren.assign_names(items)
            self.done.emit(items, "")
        except Exception:
            self.failed.emit(traceback.format_exc())


class DiskExecWorker(QThread):
    row = Signal(int, int, object)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, renamer, items):
        super().__init__()
        self.renamer, self.items = renamer, items

    def run(self):
        try:
            rep = self.renamer.execute(
                self.items, lambda i, t, p, it: self.row.emit(i, t, it)
            )
            self.done.emit(rep)
        except Exception:
            self.failed.emit(traceback.format_exc())


# =========================================================================== #
class CategoryDialog(QDialog):
    def __init__(self, parent, book: CategoryBook):
        super().__init__(parent)
        self.book = book
        self.cur = -1
        self.setWindowTitle("类别清单")
        self.resize(920, 580)
        self.setStyleSheet(QSS)

        root = QHBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(12)

        left = QVBoxLayout()
        left.addWidget(QLabel("<b>类别</b>（顺序即优先级）"))
        self.lst = QListWidget()
        self.lst.setFixedWidth(230)
        self.lst.currentRowChanged.connect(self._pick)
        left.addWidget(self.lst, 1)
        bar = QHBoxLayout()
        for t, fn in (("新增", self._add), ("删除", self._del), ("↑", self._up), ("↓", self._down)):
            b = QPushButton(t)
            b.setFixedWidth(52 if len(t) > 1 else 34)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch()
        left.addLayout(bar)
        root.addLayout(left)

        right = QVBoxLayout()
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("名称"))
        self.ed_name = QLineEdit()
        self.ed_name.setFixedWidth(160)
        r1.addWidget(self.ed_name)
        self.cb_en = QCheckBox("启用")
        r1.addWidget(self.cb_en)
        r1.addSpacing(10)
        r1.addWidget(QLabel("特殊规则"))
        self.cmb = QComboBox()
        self.cmb.addItems(["无", "animated", "screenshot", "video"])
        self.cmb.setFixedWidth(130)
        r1.addWidget(self.cmb)
        r1.addStretch()
        right.addLayout(r1)

        tip = QLabel("语义描述（英文，一行一条；写得越具体识别越准。留空则该类只靠规则命中）")
        tip.setStyleSheet(f"color:{MUTED};")
        right.addWidget(tip)
        self.txt = QPlainTextEdit()
        self.txt.setFont(QFont("Consolas", 10))
        right.addWidget(self.txt, 1)

        note = QLabel("规则说明：animated=动图(GIF/动态WEBP)　screenshot=截图(文件名或分辨率命中)　"
                      "video=视频单独归类（需在主界面勾选）")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        right.addWidget(note)

        r2 = QHBoxLayout()
        r2.addStretch()
        b_cancel = QPushButton("取消")
        b_cancel.clicked.connect(self.reject)
        b_save = QPushButton("保存并关闭")
        b_save.setObjectName("primary")
        b_save.clicked.connect(self._save)
        r2.addWidget(b_cancel)
        r2.addWidget(b_save)
        right.addLayout(r2)
        root.addLayout(right, 1)

        self._reload()
        if self.book.categories:
            self.lst.setCurrentRow(0)

    def _reload(self, keep=None):
        self.lst.blockSignals(True)
        self.lst.clear()
        for c in self.book.categories:
            txt = c.name + (f"   [{c.rule}]" if c.rule else "")
            self.lst.addItem(txt)
            it = self.lst.item(self.lst.count() - 1)
            it.setIcon(icon_dot(OKC if c.enabled else "#c7ccd8"))
            if not c.enabled:
                it.setForeground(QColor(MUTED))
        self.lst.blockSignals(False)
        if keep is not None and 0 <= keep < self.lst.count():
            self.lst.setCurrentRow(keep)

    # 类别名白名单：禁止路径分隔符 / 盘符 / UNC / 保留名 / 控制字符，
    # 防止类别名被直接拼进目标目录路径时造成路径穿越或写进系统保留名
    _INVALID_NAME_CHARS = set('\\/:*?"<>|')
    _RESERVED_NAMES = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }

    def _valid_category_name(self, name: str) -> str | None:
        """返回 None 表示合法；否则返回错误说明（用于拦截路径注入）。"""
        n = (name or "").strip()
        if not n:
            return "类别名不能为空"
        if any(ch in self._INVALID_NAME_CHARS for ch in n):
            return "类别名不能包含 \\ / : * ? \" < > | 等路径字符"
        if n in (".", ".."):
            return "类别名不能是 . 或 .."
        if n.upper() in self._RESERVED_NAMES:
            return f"「{n}」是系统保留名，不能用作类别名"
        if any(ord(ch) < 32 for ch in n):
            return "类别名不能包含控制字符"
        return None

    def _flush(self):
        if not (0 <= self.cur < len(self.book.categories)):
            return
        c = self.book.categories[self.cur]
        n = self.ed_name.text().strip()
        if n:
            err = self._valid_category_name(n)
            if err:
                QMessageBox.warning(self, "类别名不合法", err)
                self.ed_name.setText(c.name)   # 还原输入框，避免写入非法名
                return
            c.name = n
        c.enabled = self.cb_en.isChecked()
        r = self.cmb.currentText()
        c.rule = None if r == "无" else r
        c.prompts = [x.strip() for x in self.txt.toPlainText().splitlines() if x.strip()]

    def _pick(self, idx):
        if idx < 0:
            return
        self._flush()
        self.cur = idx
        c = self.book.categories[idx]
        self.ed_name.setText(c.name)
        self.cb_en.setChecked(c.enabled)
        self.cmb.setCurrentText(c.rule or "无")
        self.txt.setPlainText("\n".join(c.prompts))

    def _add(self):
        self._flush()
        self.book.categories.append(Category(name="新类别", enabled=True, rule=None, prompts=[]))
        self._reload(len(self.book.categories) - 1)

    def _del(self):
        if not (0 <= self.cur < len(self.book.categories)):
            return
        name = self.book.categories[self.cur].name
        if QMessageBox.question(self, "确认",
                                f"删除类别「{name}」？\n（只删清单，已归好的文件夹不动）") != QMessageBox.Yes:
            return
        self.book.categories.pop(self.cur)
        self.cur = -1
        self._reload(0)

    def _move(self, d):
        j = self.cur + d
        if not (0 <= self.cur < len(self.book.categories) and 0 <= j < len(self.book.categories)):
            return
        self._flush()
        cs = self.book.categories
        cs[self.cur], cs[j] = cs[j], cs[self.cur]
        self.cur = j
        self._reload(j)

    def _up(self):
        self._move(-1)

    def _down(self):
        self._move(1)

    def _save(self):
        self._flush()
        seen = set()
        for c in self.book.categories:
            err = self._valid_category_name(c.name)
            if err:
                QMessageBox.warning(self, "类别名不合法", f"「{c.name}」{err}")
                return
            if c.name in seen:
                QMessageBox.warning(self, "类别重名", f"「{c.name}」出现了两次，请改掉。")
                return
            seen.add(c.name)
        try:
            self.book.save()
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))
            return
        self.accept()


# =========================================================================== #
class DropArea(QFrame):
    dropped = Signal(str)
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("drop")
        self.setAcceptDrops(True)
        self.setFixedHeight(104)
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setSpacing(4)
        self.title = QLabel("把文件夹拖到这里")
        self.title.setObjectName("dropTitle")
        self.title.setAlignment(Qt.AlignCenter)
        self.sub = QLabel("也可以点击选择　·　支持 JPG/PNG/GIF/WEBP/RAW　·　MP4/MOV/FLV")
        self.sub.setObjectName("dropSub")
        self.sub.setAlignment(Qt.AlignCenter)
        lay.addStretch()
        lay.addWidget(self.title)
        lay.addWidget(self.sub)
        lay.addStretch()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.setObjectName("dropHot")
            self.setStyleSheet(QSS)

    def dragLeaveEvent(self, e):
        self.setObjectName("drop")
        self.setStyleSheet(QSS)

    def dropEvent(self, e):
        self.setObjectName("drop")
        self.setStyleSheet(QSS)
        urls = e.mimeData().urls()
        if urls:
            self.dropped.emit(urls[0].toLocalFile())

    def mousePressEvent(self, e):
        self.clicked.emit()


# =========================================================================== #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._settings_warn = ""
        try:
            self.book = CategoryBook.load()
        except Exception as e:
            # categories.json 多半是手改时多/少了个逗号，别让程序直接起不来
            QMessageBox.critical(
                None, "类别清单读不出来",
                f"{type(e).__name__}: {e}\n\n"
                "先用空清单启动：你可以在「编辑类别清单」里重新添加，"
                "或者检查程序目录下的 categories.json 是不是被改坏了。"
            )
            self.book = CategoryBook(categories=[])
        try:
            self.settings = Settings.load()
        except Exception as e:
            # 配置文件坏了：先用默认设置把程序开起来，稍后明确告诉用户，绝不闷声回默认
            self.settings = Settings()
            self._settings_warn = str(e)
        self.folder = ""
        self.mode = self.settings.mode
        self.roots: list[str] = list(self.settings.scan_roots)
        self.items: list = []
        self._item_index: dict[int, int] = {}
        self.scan_worker: ScanWorker | None = None
        self.exec_worker: ExecWorker | None = None
        self.disk_renamer: InPlaceRenamer | None = None
        self.disk_scan_worker: "DiskScanWorker | None" = None
        self.disk_exec_worker: "DiskExecWorker | None" = None
        self.organizer: Organizer | None = None
        self.busy = False

        self.setWindowTitle(APP_TITLE)
        self.resize(1120, 740)
        self.setMinimumSize(940, 620)
        self.setStyleSheet(QSS)
        self._build()
        self._engine_hint()
        if self._settings_warn:
            QMessageBox.warning(
                self, "设置已重置",
                f"{self._settings_warn}\n\n"
                "本次先用默认设置启动。点「开始识别」或关闭程序时，会自动重新存一份好的配置文件。"
            )

    # ------------------------------------------------------------------ UI
    def _build(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(10)

        self.drop = DropArea()
        self.drop.dropped.connect(self._set_folder)
        self.drop.clicked.connect(self._pick_folder)
        v.addWidget(self.drop)

        # ---- 模式切换 ----
        mw = QWidget()
        mh = QHBoxLayout(mw)
        mh.setContentsMargins(0, 0, 0, 0)
        mh.addWidget(QLabel("<b>模式</b>"))
        self.cmb_mode = QComboBox()
        for label, val in (("归类整理（移入分类文件夹）", "organize"),
                           ("原位重命名（只改名、不移动）", "inplace")):
            self.cmb_mode.addItem(label, val)
        self._set_combo(self.cmb_mode, self.settings.mode)
        self.cmb_mode.currentIndexChanged.connect(self._toggle_mode)
        mh.addWidget(self.cmb_mode)
        mh.addSpacing(14)
        mh.addWidget(QLabel("分类结果面板（点击定位）："))
        self.lst_cat = QListWidget()
        self.lst_cat.setFixedHeight(30)
        self.lst_cat.setFlow(QListWidget.LeftToRight)
        self.lst_cat.setWrapping(False)
        self.lst_cat.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lst_cat.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.lst_cat.itemClicked.connect(self._filter_cat)
        self.lst_cat.setVisible(False)
        mh.addWidget(self.lst_cat, 1)
        self.b_csv = QPushButton("导出清单 CSV")
        self.b_csv.clicked.connect(self._export_csv)
        self.b_csv.setEnabled(False)
        self.b_csv.setVisible(False)
        mh.addWidget(self.b_csv)
        v.addWidget(mw)

        # ---- 原位重命名：磁盘根目录卡片 ----
        self.disk_card = QFrame()
        self.disk_card.setObjectName("card")
        dv = QVBoxLayout(self.disk_card)
        dv.setContentsMargins(14, 10, 14, 10)
        dv.setSpacing(8)
        dh = QHBoxLayout()
        dh.addWidget(QLabel("<b>扫描根目录</b>（勾选要扫的盘 / 文件夹）"))
        self.b_add_root = QPushButton("+ 添加")
        self.b_add_root.clicked.connect(self._add_root)
        self.b_rm_root = QPushButton("- 移除")
        self.b_rm_root.clicked.connect(self._rm_root)
        dh.addWidget(self.b_add_root)
        dh.addWidget(self.b_rm_root)
        dh.addSpacing(14)
        dh.addWidget(QLabel("日期粒度"))
        self.cmb_inplace_g = QComboBox()
        for label, val in (("年（2023.风景.01）", "year"), ("年.月.日（2023.08.15.风景.01）", "ymd")):
            self.cmb_inplace_g.addItem(label, val)
        self._set_combo(self.cmb_inplace_g, self.settings.inplace_date_granularity)
        dh.addWidget(self.cmb_inplace_g)
        dh.addStretch()
        dv.addLayout(dh)
        self.lst_roots = QListWidget()
        self.lst_roots.setFixedHeight(72)
        dv.addWidget(self.lst_roots)
        v.addWidget(self.disk_card)

        # ---- 参数栏 ----
        opt = QFrame()
        opt.setObjectName("card")
        vopt = QVBoxLayout(opt)
        vopt.setContentsMargins(14, 10, 14, 10)
        vopt.setSpacing(8)

        # 行1：命名模式（日期段 / 期日编号 / 时间板块）
        h1 = QHBoxLayout()
        h1.setSpacing(10)
        h1.addWidget(QLabel("<b>日期段</b>"))
        self.ed_date = QLineEdit(self.settings.date_tag)
        self.ed_date.setFixedWidth(70)
        self.ed_date.setPlaceholderText("留空=纯序号")
        self.ed_date.textChanged.connect(self._sample)
        h1.addWidget(self.ed_date)

        self.cb_date = QCheckBox("期日编号（自动取创建时间）")
        self.cb_date.setChecked(self.settings.date_numbering)
        self.cb_date.stateChanged.connect(self._sample)
        self.cb_date.stateChanged.connect(self._toggle_date_inputs)
        h1.addWidget(self.cb_date)
        self.cmb_date_g = QComboBox()
        for label, val in (("年", "year"), ("年-月", "month"), ("年-月-日", "day")):
            self.cmb_date_g.addItem(label, val)
        self._set_combo(self.cmb_date_g, self.settings.date_granularity)
        self.cmb_date_g.setFixedWidth(90)
        self.cmb_date_g.currentIndexChanged.connect(self._sample)
        h1.addWidget(self.cmb_date_g)

        h1.addSpacing(10)
        h1.addWidget(QLabel("日期来源"))
        self.cmb_date_src = QComboBox()
        for label, val in (("拍摄时间(EXIF)", "exif"), ("文件创建时间", "ctime")):
            self.cmb_date_src.addItem(label, val)
        self._set_combo(self.cmb_date_src, self.settings.date_source)
        self.cmb_date_src.setFixedWidth(130)
        self.cmb_date_src.setToolTip(
            "拍摄时间(EXIF)：用照片本身记录的拍摄日期，从手机/相机/网盘复制来的照片最准；\n"
            "文件创建时间：用 Windows 文件属性里的「创建时间」（通常是导入本机的时间）。"
        )
        h1.addWidget(self.cmb_date_src)

        h1.addSpacing(10)
        self.cb_block = QCheckBox("时间板块（按时间分目录）")
        self.cb_block.setChecked(self.settings.time_block)
        self.cb_block.stateChanged.connect(self._sample)
        h1.addWidget(self.cb_block)
        self.cmb_block_g = QComboBox()
        for label, val in (("按年", "year"), ("按年-月", "month")):
            self.cmb_block_g.addItem(label, val)
        self._set_combo(self.cmb_block_g, self.settings.time_block_granularity)
        self.cmb_block_g.setFixedWidth(80)
        self.cmb_block_g.currentIndexChanged.connect(self._sample)
        h1.addWidget(self.cmb_block_g)

        self.lb_sample = QLabel()
        self.lb_sample.setStyleSheet(f"color:{ACCENT};")
        h1.addWidget(self.lb_sample)
        h1.addStretch()
        vopt.addLayout(h1)

        # 行2：其它开关
        h2 = QHBoxLayout()
        h2.setSpacing(10)
        self.cb_rec = QCheckBox("连子文件夹一起整理")
        self.cb_rec.setChecked(self.settings.recursive)
        h2.addWidget(self.cb_rec)
        self.cb_vid = QCheckBox("视频单独归类")
        self.cb_vid.setChecked(self.settings.video_as_own_category)
        h2.addWidget(self.cb_vid)
        self.cb_keep = QCheckBox("新名保留原文件名")
        self.cb_keep.setChecked(self.settings.keep_original_name)
        self.cb_keep.stateChanged.connect(self._sample)
        h2.addWidget(self.cb_keep)

        self.cb_ai = QCheckBox("语义识别（本地视觉模型）")
        self.cb_ai.setChecked(self.settings.use_ai)
        self.cb_ai.setToolTip(
            "开着：用本地 CLIP 模型看懂画面内容来分类（离线、0 积分），首次加载约需十几秒。\n"
            "关掉：只按规则整理（动图 / 截图 / 视频），其余归到「其它」，不加载模型、更省内存。"
        )
        self.cb_ai.stateChanged.connect(lambda _s: self._engine_hint())
        h2.addWidget(self.cb_ai)

        h2.addSpacing(8)
        h2.addWidget(QLabel("把握阈值"))
        self.sl = QSlider(Qt.Horizontal)
        self.sl.setRange(10, 60)
        self.sl.setValue(int(self.settings.min_confidence * 100))
        self.sl.setFixedWidth(110)
        self.sl.valueChanged.connect(lambda x: self.lb_conf.setText(f"{x/100:.2f}"))
        h2.addWidget(self.sl)
        self.lb_conf = QLabel(f"{self.settings.min_confidence:.2f}")
        self.lb_conf.setFixedWidth(34)
        h2.addWidget(self.lb_conf)

        h2.addStretch()
        b_cat = QPushButton("编辑类别清单")
        b_cat.clicked.connect(self._edit_cats)
        h2.addWidget(b_cat)
        vopt.addLayout(h2)

        v.addWidget(opt)

        # ---- 动作栏 ----
        a = QHBoxLayout()
        self.b_scan = QPushButton("① 开始识别（不动文件）")
        self.b_scan.setObjectName("primary")
        self.b_scan.setEnabled(False)
        self.b_scan.clicked.connect(self._start_scan)
        a.addWidget(self.b_scan)
        self.b_run = QPushButton("② 执行归档")
        self.b_run.setObjectName("go")
        self.b_run.setEnabled(False)
        self.b_run.clicked.connect(self._start_exec)
        a.addWidget(self.b_run)
        self.b_stop = QPushButton("停止")
        self.b_stop.setEnabled(False)
        self.b_stop.clicked.connect(self._stop)
        a.addWidget(self.b_stop)
        a.addStretch()
        self.b_undo = QPushButton("撤销上一步")
        self.b_undo.setObjectName("danger")
        self.b_undo.setEnabled(False)
        self.b_undo.clicked.connect(self._undo_last)
        a.addWidget(self.b_undo)
        self.b_undo_hist = QPushButton("撤销到… ▾")
        self.b_undo_hist.setObjectName("danger")
        self.b_undo_hist.setEnabled(False)
        self.undo_menu = QMenu(self)
        self.b_undo_hist.setMenu(self.undo_menu)
        a.addWidget(self.b_undo_hist)
        v.addLayout(a)

        # ---- 表格 ----
        self.tb = QTableWidget(0, 5)
        self.tb.setHorizontalHeaderLabels(["原文件", "识别类别", "依据", "新文件名", "状态"])
        self.tb.verticalHeader().setVisible(False)
        self.tb.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tb.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tb.setAlternatingRowColors(False)
        hh = self.tb.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.Fixed)
        hh.setSectionResizeMode(2, QHeaderView.Fixed)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.Fixed)
        self.tb.setColumnWidth(1, 100)
        self.tb.setColumnWidth(2, 150)
        self.tb.setColumnWidth(4, 150)
        self.tb.cellDoubleClicked.connect(self._edit_row)
        v.addWidget(self.tb, 1)

        hint = QLabel("小技巧：双击任意一行可以手动改它的类别，改完会自动重排编号。")
        hint.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        v.addWidget(hint)

        # ---- 底部 ----
        b = QHBoxLayout()
        self.pb = QProgressBar()
        self.pb.setFixedWidth(240)
        self.pb.setTextVisible(False)
        b.addWidget(self.pb)
        self.lb_status = QLabel("就绪　·　把要整理的文件夹拖进来")
        self.lb_status.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        b.addWidget(self.lb_status, 1)
        self.lb_engine = QLabel()
        self.lb_engine.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        b.addWidget(self.lb_engine)
        v.addLayout(b)

        self._toggle_date_inputs()
        self._toggle_mode()
        self._sample()

    # ------------------------------------------------------------------ 小工具
    def _engine_hint(self):
        cb = getattr(self, "cb_ai", None)
        if cb is not None and not cb.isChecked():
            self.lb_engine.setText("语义识别：已关闭（仅按规则整理）")
            self.lb_engine.setStyleSheet(f"color:{MUTED}; font-size:11px;")
            return
        if SemanticEngine.deps_installed():
            self.lb_engine.setText("语义识别：本地模型 · 离线 · 0 积分")
            self.lb_engine.setStyleSheet(f"color:{OKC}; font-size:11px;")
        else:
            self.lb_engine.setText("语义识别：未安装（仅规则整理）")
            self.lb_engine.setStyleSheet(f"color:{WARNC}; font-size:11px;")

    def _set_combo(self, cmb: QComboBox, value) -> None:
        idx = cmb.findData(value)
        if idx >= 0:
            cmb.setCurrentIndex(idx)

    def _toggle_date_inputs(self):
        # 期日编号开启时，手动日期段输入框就用不上了，禁用以避免误会
        self.ed_date.setDisabled(self.cb_date.isChecked())

    # ------------------------------------------------------------------ 模式 / 磁盘根目录
    def _toggle_mode(self):
        self.mode = self.cmb_mode.currentData()
        inplace = self.mode == "inplace"
        self.drop.setVisible(not inplace)
        self.disk_card.setVisible(inplace)
        self.lst_cat.setVisible(False)
        self.b_csv.setVisible(False)
        if inplace:
            self.b_run.setText("② 执行重命名（原地）")
            self.b_scan.setText("① 扫描并识别（不动文件）")
        else:
            self.b_run.setText("② 执行归档")
            self.b_scan.setText("① 开始识别（不动文件）")
        self._refresh_roots_ui()
        self._update_scan_enabled()

    def _refresh_roots_ui(self):
        self.lst_roots.blockSignals(True)
        self.lst_roots.clear()
        for r in self.roots:
            self.lst_roots.addItem(r)
        self.lst_roots.blockSignals(False)

    def _add_root(self):
        if self.busy:
            return
        init = self.roots[-1] if self.roots else ""
        p = QFileDialog.getExistingDirectory(self, "选择要扫描的磁盘 / 文件夹", init)
        if not p:
            return
        ap = os.path.abspath(p)
        if ap in [os.path.abspath(x) for x in self.roots]:
            return
        # 拦一道：系统盘/系统目录整盘扫会钻进 Windows\WinSxS 这类几十万文件的目录，
        # 扫一天也扫不完，还容易撞权限错误。真要扫就让用户明确确认一次。
        warn = _risky_root_reason(ap)
        if warn and QMessageBox.question(
                self, "确认扫描这个目录",
                f"{warn}\n\n仍要把它加入扫描列表吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.roots.append(p)
        self._refresh_roots_ui()
        self._update_scan_enabled()

    def _rm_root(self):
        if self.busy:
            return
        # 必须按行号「从大到小」删。正序删会让列表左移，
        # 选中第 0 和第 2 行时，删完 0 之后原本的 2 已经跑到 1，
        # 再 pop(2) 删掉的其实是原来的第 3 项——删错人。
        rows = sorted({i.row() for i in self.lst_roots.selectedIndexes()}, reverse=True)
        for r in rows:
            try:
                self.roots.pop(r)
            except IndexError:
                pass
        self._refresh_roots_ui()
        self._update_scan_enabled()

    def _update_scan_enabled(self):
        if self.mode == "inplace":
            self.b_scan.setEnabled(not self.busy and bool(self.roots))
        else:
            self.b_scan.setEnabled(not self.busy and bool(self.folder))

    # ------------------------------------------------------------------ 分类结果面板
    def _populate_cat_panel(self, items):
        self.lst_cat.blockSignals(True)
        self.lst_cat.clear()
        self.lst_cat.addItem("全部")
        stat: dict[str, int] = {}
        for it in items:
            stat[it.category] = stat.get(it.category, 0) + 1
        for k in sorted(stat, key=lambda x: -stat[x]):
            self.lst_cat.addItem(f"{k} ×{stat[k]}")
        self.lst_cat.blockSignals(False)
        self.lst_cat.setVisible(True)
        self.b_csv.setVisible(True)
        self.b_csv.setEnabled(True)

    def _filter_cat(self, item):
        txt = item.text()
        cat = txt.split(" ×")[0] if txt != "全部" else None
        for r in range(self.tb.rowCount()):
            it = self.items[r] if 0 <= r < len(self.items) else None
            show = (cat is None) or (it is not None and it.category == cat)
            self.tb.setRowHidden(r, not show)

    def _export_csv(self):
        if not self.items:
            return
        init = self.roots[0] if self.roots else (self.folder or "")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出清单", os.path.join(init, "photo_sorter_report.csv"), "CSV 文件 (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        if os.path.exists(path):
            if QMessageBox.question(
                    self, "文件已存在",
                    f"「{os.path.basename(path)}」已存在，覆盖它？\n"
                    "（导出清单只写 CSV，不会碰你的照片，但会覆盖这个文件）"
            ) != QMessageBox.Yes:
                return
        try:
            import csv
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["原路径", "识别类别", "依据", "新文件名", "时间来源", "置信度"])
                for it in self.items:
                    w.writerow([it.src, it.category, it.reason, it.new_name or "",
                                it.time_source or "", f"{it.confidence:.2f}"])
            self._status(f"清单已导出：{os.path.basename(path)}", OKC)
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _open_loc(self, path):
        d = os.path.dirname(path)
        try:
            # os.startfile 仅 Windows 可用；其它平台用系统默认文件管理器打开
            if sys.platform == "win32":
                os.startfile(d)
            elif sys.platform == "darwin":
                subprocess.run(["open", d], check=False)
            else:
                subprocess.run(["xdg-open", d], check=False)
        except Exception:
            QMessageBox.information(self, "打开文件夹", d)

    # ------------------------------------------------------------------ 磁盘扫描 / 重命名
    def _start_disk_scan(self):
        if self.busy or not self.roots:
            return
        self._sync()
        self.tb.setRowCount(0)
        self.items = []
        self._set_busy(True)
        w = DiskScanWorker(self.book, self.settings, self.roots)
        w.status.connect(lambda m: self._status(m))
        w.total.connect(lambda n: (self.pb.setMaximum(max(1, n)), self.pb.setValue(0)))
        w.row.connect(self._on_scan_row)
        w.done.connect(self._on_disk_scan_done)
        w.failed.connect(self._on_fail)
        self.disk_scan_worker = w
        w.start()

    def _on_disk_scan_done(self, items, warn):
        if items:
            self.items = items
        self._item_index = {id(x): i for i, x in enumerate(self.items)}
        self._refill()
        self.disk_renamer = self.disk_scan_worker.renamer if self.disk_scan_worker else None
        self._set_busy(False)
        if warn:
            self._status(warn, WARNC)
            return
        stat: dict[str, int] = {}
        for it in self.items:
            stat[it.category] = stat.get(it.category, 0) + 1
        brief = "　".join(f"{k}×{v}" for k, v in sorted(stat.items(), key=lambda x: -x[1]))
        self._status(f"识别完成，共 {len(self.items)} 个：{brief}　→ 核对无误后点「执行重命名」", OKC)
        self._populate_cat_panel(self.items)
        self.b_run.setEnabled(True)

    def _start_disk_exec(self):
        if self.busy or not self.items or not self.disk_renamer:
            return
        todo = [i for i in self.items if i.status == "待处理"]
        if not todo:
            QMessageBox.information(self, "无事可做", "没有待处理的文件。")
            return
        if not self._time_report(todo):
            return
        stat: dict[str, int] = {}
        for i in todo:
            stat[i.category] = stat.get(i.category, 0) + 1
        lines = "\n".join(f"　{k} → {v} 个" for k, v in sorted(stat.items(), key=lambda x: -x[1]))
        msg = (f"即将在照片「原位」改名（不移动、不建文件夹）{len(todo)} 个文件：\n\n{lines}\n\n"
               "· 只改名，不移动、不重新编码、不压缩画质\n"
               "· 遇到重名一律自动换号，绝不覆盖任何文件\n"
               "· 改完可以一键撤销\n\n确定执行？")
        if QMessageBox.question(self, "确认重命名", msg,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
            return
        self._set_busy(True)
        self.pb.setMaximum(max(1, len(self.items)))
        w = DiskExecWorker(self.disk_renamer, self.items)
        w.row.connect(self._on_exec_row)
        w.done.connect(self._on_disk_exec_done)
        w.failed.connect(self._on_fail)
        self.disk_exec_worker = w
        w.start()

    def _on_disk_exec_done(self, rep):
        self._set_busy(False)
        self.b_run.setEnabled(False)
        self._refresh_undo_menu()
        self._status(f"重命名完成：成功 {rep.moved}，跳过 {rep.skipped}，失败 {rep.failed}", OKC)
        lines = "\n".join(f"　{k} → {v} 个" for k, v in sorted(rep.by_category.items(), key=lambda x: -x[1]))
        detail = (f"成功 {rep.moved} 个，跳过 {rep.skipped} 个，失败 {rep.failed} 个。\n\n{lines}\n\n"
                  "原文件画质、格式、拍摄信息全部原样保留，只是文件名变了。\n"
                  "不满意可点「撤销上一步」或「撤销到…」还原。")
        if rep.warning:
            detail += "\n\n⚠ " + rep.warning
            self._status("重命名完成 · 注意：撤销记录写入失败（详见弹窗）", WARNC)
        QMessageBox.information(self, "重命名完成", detail)

    # ------------------------------------------------------------------ 撤销（跨模式）
    def _undo_roots(self):
        return self.roots if self.mode == "inplace" else [self.folder]

    def _list_undo(self):
        """只列「当前模式」产生的撤销记录，避免两种模式的记录互相串台。"""
        if self.mode == "inplace":
            return list_disk_undo_files(self.roots, kind="inplace")
        return list_undo_files(self.folder, kind="organize") if self.folder else []

    def _after_undo_refresh(self):
        self._refresh_undo_menu()
        self._update_scan_enabled()

    def _sample(self):
        keep = self.cb_keep.isChecked()
        block = self.cb_block.isChecked()

        # 文件名上的日期段（手动日期段 或 按 date_granularity 自动取）
        if self.cb_date.isChecked():
            g = self.cmb_date_g.currentData()
            fname_date = {"year": "2023", "month": "2023-05", "day": "2023-05-12"}.get(g, "2023")
        else:
            fname_date = self.ed_date.text().strip()

        # 文件夹名：勾时间板块时按 time_block_granularity（年→2023人像 / 月→2023-05人像）
        if block:
            bg = self.cmb_block_g.currentData()
            tb = {"year": "2023", "month": "2023-05"}.get(bg, "2023")
            folder = f"{tb}人像/"
        else:
            folder = "人像/"

        # 文件名：始终带日期段（日期显示在图片本体上，便于同文件夹内不同月份照片区分）
        if fname_date:
            fname = f"人像{fname_date}-01"
        else:
            fname = "人像01"
        if keep:
            fname += "_IMG_2233"

        tail = "（日期显示在文件名上）" if fname_date else "（未设日期段：文件名不带日期）"
        self.lb_sample.setText(f"示例：{folder}{fname}.jpg  {tail}")

    def _status(self, t, color=TEXT):
        self.lb_status.setText(t)
        self.lb_status.setStyleSheet(f"color:{color};")

    def _detail(self, title: str, head: str, body: str) -> None:
        """通用明细窗口：内容可滚动、可选中复制，再长也不会被一刀切掉。"""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(680, 460)
        v = QVBoxLayout(dlg)
        lb = QLabel(head)
        lb.setWordWrap(True)
        v.addWidget(lb)
        te = QPlainTextEdit(body)
        te.setReadOnly(True)
        te.setFont(QFont("Consolas", 10))
        v.addWidget(te, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.button(QDialogButtonBox.Close).setText("关闭")
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        dlg.exec()

    def _sync(self) -> bool:
        """把界面上的选择记进设置并保存。返回 False 表示没存上（调用方负责提示）。"""
        self.settings.date_tag = self.ed_date.text().strip()
        self.settings.recursive = self.cb_rec.isChecked()
        self.settings.video_as_own_category = self.cb_vid.isChecked()
        self.settings.keep_original_name = self.cb_keep.isChecked()
        self.settings.min_confidence = self.sl.value() / 100.0
        self.settings.date_numbering = self.cb_date.isChecked()
        self.settings.date_granularity = self.cmb_date_g.currentData()
        self.settings.date_source = self.cmb_date_src.currentData()
        self.settings.time_block = self.cb_block.isChecked()
        self.settings.time_block_granularity = self.cmb_block_g.currentData()
        self.settings.use_ai = self.cb_ai.isChecked()
        self.settings.mode = self.mode
        self.settings.inplace_date_granularity = self.cmb_inplace_g.currentData()
        self.settings.scan_roots = list(self.roots)
        self.settings.last_folder = self.folder
        self.book.min_confidence = self.settings.min_confidence
        try:
            self.settings.save()
            return True
        except Exception as e:
            # 存不上不影响本次整理，但得让用户看见，不然下次启动莫名其妙回默认
            self._status(f"设置保存失败：{type(e).__name__}: {e}", WARNC)
            return False

    # ------------------------------------------------------------------ 选目录
    def _pick_folder(self):
        if self.busy:
            return
        init = self.settings.last_folder if os.path.isdir(self.settings.last_folder) else ""
        p = QFileDialog.getExistingDirectory(self, "选择要整理的文件夹", init)
        if p:
            self._set_folder(p)

    def _set_folder(self, p: str):
        if self.busy:
            return
        p = os.path.abspath(p)
        if os.path.isfile(p):
            p = os.path.dirname(p)
        if not os.path.isdir(p):
            QMessageBox.warning(self, "不是文件夹", f"请拖入一个文件夹：\n{p}")
            return
        self.folder = p
        self.drop.title.setText(os.path.basename(p) or p)
        try:
            n = sum(1 for e in os.scandir(p)
                    if e.is_file() and os.path.splitext(e.name)[1].lower() in ALL_EXTS)
        except OSError:
            n = 0
        self.drop.sub.setText(f"{p}　·　根目录下 {n} 个媒体文件")
        self.b_scan.setEnabled(True)
        self.b_run.setEnabled(False)
        self.b_undo.setEnabled(bool(list_undo_files(p)))
        self._refresh_undo_menu()
        self.tb.setRowCount(0)
        self.items = []
        self._item_index = {}
        self._status("已选中文件夹　·　先点「开始识别」看结果，确认无误再归档")

    # ------------------------------------------------------------------ 识别
    def _start_scan(self):
        if self.busy:
            return
        if self.mode == "inplace":
            self._start_disk_scan()
            return
        if not self.folder:
            return
        self._sync()
        self.tb.setRowCount(0)
        self.items = []
        self._set_busy(True)
        w = ScanWorker(self.book, self.settings, self.folder)
        w.status.connect(lambda m: self._status(m))
        w.total.connect(lambda n: (self.pb.setMaximum(max(1, n)), self.pb.setValue(0)))
        w.row.connect(self._on_scan_row)
        w.done.connect(self._on_scan_done)
        w.failed.connect(self._on_fail)
        self.scan_worker = w
        w.start()

    def _on_scan_row(self, i, total, item):
        self.items.append(item)
        self._item_index = getattr(self, "_item_index", {})
        self._item_index[id(item)] = len(self.items) - 1
        self._fill(len(self.items) - 1, item)
        self.pb.setValue(i)
        self._status(f"识别中 {i}/{total}　·　{os.path.basename(item.src)} → {item.category}")
        if i % 4 == 0:
            self.tb.scrollToBottom()

    def _on_scan_done(self, items, warn):
        if items:
            self.items = items
        self._item_index = {id(x): i for i, x in enumerate(self.items)}
        self._refill()
        self.organizer = self.scan_worker.organizer if self.scan_worker else None
        self._set_busy(False)
        if warn:
            self._status(warn, WARNC)
            return
        stat = {}
        for it in self.items:
            stat[it.category] = stat.get(it.category, 0) + 1
        brief = "　".join(f"{k}×{v}" for k, v in sorted(stat.items(), key=lambda x: -x[1]))
        self._status(f"识别完成，共 {len(self.items)} 个：{brief}　→ 核对无误后点「执行归档」", OKC)
        self._populate_cat_panel(self.items)
        self.b_run.setEnabled(True)

    # ------------------------------------------------------------------ 时间来源报告
    def _time_report(self, items) -> bool:
        """归档前弹窗：展示每张照片的拍摄时间来自哪里，让用户核对日期真实。

        返回 False 表示用户取消（不归档）。
        """
        src_count: dict[str, int] = {}
        for it in items:
            k = it.time_source or "unknown"
            src_count[k] = src_count.get(k, 0) + 1
        label = {"exif": "EXIF 拍摄时间", "filename": "文件名解析日期", "filetime": "文件时间兜底",
                 "ctime": "文件创建时间", "video_meta": "视频内置时间", "unknown": "无法获取"}
        total = len(items)
        lines = [f"共 {total} 个文件，时间来源分布："]
        for k in ("exif", "filename", "video_meta", "filetime", "ctime", "unknown"):
            if src_count.get(k):
                lines.append(f"  · {label.get(k, k)}：{src_count[k]} 个")

        # 需重点核对的（不是照片真实拍摄时间）
        if self.settings.date_source == "exif":
            watch = [it for it in items if it.time_source in ("filename", "filetime", "unknown")]
        else:
            watch = [it for it in items if it.time_source in ("filetime", "unknown")]
        if watch:
            lines.append("")
            lines.append(f"⚠ 以下 {len(watch)} 个不是来自照片真实拍摄时间（EXIF），日期可能只是导入/复制时间，请重点核对：")
            for it in watch[:300]:
                dt = datetime.fromtimestamp(it.shot_time).strftime("%Y-%m-%d") if it.shot_time else "无"
                lines.append(f"  · {os.path.basename(it.src)} → {dt}（{label.get(it.time_source, it.time_source)}）")
            if len(watch) > 300:
                lines.append(f"  … 还有 {len(watch) - 300} 个未列出")
        else:
            lines.append("")
            lines.append("✓ 全部来自 EXIF 真实拍摄时间，日期可信。")
        lines.append("")
        lines.append("确认无误点「确认执行」；若发现日期不对，可改「日期来源」或先取消。")

        dlg = QDialog(self)
        dlg.setWindowTitle("时间来源核对（归档前）")
        dlg.resize(660, 480)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("归档前请先确认每张照片的「拍摄时间」来自哪里："))
        te = QPlainTextEdit("\n".join(lines))
        te.setReadOnly(True)
        v.addWidget(te, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("确认执行")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        return dlg.exec() == QDialog.Accepted

    # ------------------------------------------------------------------ 执行
    def _start_exec(self):
        if self.busy:
            return
        if self.mode == "inplace":
            self._start_disk_exec()
            return
        if not self.items or not self.organizer:
            return
        todo = [i for i in self.items if i.status == "待处理"]
        if not todo:
            QMessageBox.information(self, "无事可做", "没有待处理的文件。")
            return
        # —— 归档前：时间来源核对，确认每张照片的拍摄日期真实 ——
        if not self._time_report(todo):
            return
        stat = {}
        for i in todo:
            stat[i.category] = stat.get(i.category, 0) + 1
        lines = "\n".join(f"　{k} → {v} 个" for k, v in sorted(stat.items(), key=lambda x: -x[1]))
        msg = (f"即将在\n{self.folder}\n下建立类别文件夹，并移动 {len(todo)} 个文件：\n\n{lines}\n\n"
               "· 只移动和改名，不会重新编码、不会压缩画质\n"
               "· 遇到重名一律自动换号，绝不覆盖任何文件\n"
               "· 整理完可以一键撤销\n\n确定执行？")
        if QMessageBox.question(self, "确认归档", msg,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
            return
        self._set_busy(True)
        self.pb.setMaximum(max(1, len(self.items)))
        w = ExecWorker(self.organizer, self.folder, self.items)
        w.row.connect(self._on_exec_row)
        w.done.connect(self._on_exec_done)
        w.failed.connect(self._on_fail)
        self.exec_worker = w
        w.start()

    def _on_exec_row(self, i, total, item):
        self.pb.setValue(i)
        idx = self._item_index.get(id(item), -1)
        if idx >= 0:
            self._fill(idx, item)
        self._status(f"归档中 {i}/{total} …")

    def _on_exec_done(self, rep):
        self._set_busy(False)
        self.b_run.setEnabled(False)
        self._refresh_undo_menu()
        self._status(f"归档完成：成功 {rep.moved}，跳过 {rep.skipped}，失败 {rep.failed}", OKC)
        lines = "\n".join(f"　{k} → {v} 个" for k, v in sorted(rep.by_category.items(), key=lambda x: -x[1]))
        detail = (
            f"成功 {rep.moved} 个，跳过 {rep.skipped} 个，失败 {rep.failed} 个。\n\n{lines}\n\n"
            "原文件画质、格式、拍摄信息全部原样保留。\n不满意可点「撤销上一步」或「撤销到…」还原到任意一步。"
        )
        if rep.warning:
            detail += "\n\n⚠ " + rep.warning
            self._status("归档完成 · 注意：撤销记录写入失败（详见弹窗）", WARNC)
        QMessageBox.information(self, "整理完成", detail)

    def _on_fail(self, tb):
        self._set_busy(False)
        self._status("出错了", ERRC)
        # 头尾都留：开头常有 ImportError 这类真正的根因，末尾才是崩溃点，只看一半会看错病
        if len(tb) > 1600:
            tb = tb[:800] + "\n\n…（中间部分已省略）…\n\n" + tb[-800:]
        self._detail("出错了", "整理已停止。详细信息如下（可滚动查看、Ctrl+C 复制给开发者）：", tb)

    def _stop(self):
        if self.organizer:
            self.organizer.stop_flag.set()
        if self.disk_renamer:
            self.disk_renamer.stop_flag.set()
        if self.scan_worker and self.scan_worker.organizer:
            self.scan_worker.organizer.stop_flag.set()
        self._status("正在停止…", WARNC)

    # ------------------------------------------------------------------ 撤销
    def _refresh_undo_menu(self):
        """刷新「撤销到…」菜单：列出最近最多 5 次归档记录，并可一键还原到任意一步之前。"""
        self.undo_menu.clear()
        files = self._list_undo()[:5]   # 最多展示最近 5 次
        self.b_undo_hist.setEnabled(bool(files))
        self.b_undo.setEnabled(bool(files))
        if not files:
            return
        for f in files:
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    moves = json.load(fh).get("moves", [])
            except Exception:
                moves = []
            t = datetime.fromtimestamp(os.path.getmtime(f)).strftime("%m-%d %H:%M")
            act = self.undo_menu.addAction(f"到 {t}（{len(moves)} 个）")
            act.setData(f)
            act.triggered.connect(lambda _checked=False, ff=f: self._undo_to(ff))

    def _undo_last(self):
        files = self._list_undo()
        if not files:
            QMessageBox.information(self, "没有记录", "这个文件夹没有可撤销的整理记录。")
            return
        latest = files[0]
        if QMessageBox.question(self, "撤销上一步",
                                f"把最近一次整理的文件全部搬回原位？\n\n记录：{os.path.basename(latest)}"
                                ) != QMessageBox.Yes:
            return
        # 原位重命名模式必须使用专用的磁盘撤销入口 undo_disk()，
        # 否则会误用归类整理模式的 undo()，导致跨模式解释记录、撤销不完整甚至搬错文件。
        if self.mode == "inplace":
            ok, fail, errs = undo_disk(self.roots)
        else:
            ok, fail, errs = undo(latest)
        self._show_undo_result(ok, fail, errs)
        if self.mode == "inplace":
            self._after_undo_refresh()
        else:
            self._set_folder(self.folder)   # 内部会刷新菜单与按钮

    def _undo_to(self, target_file: str):
        if QMessageBox.question(self, "撤销到指定步",
                                "将把这一次以及其后的所有整理，一次性还原到该步操作之前的状态？\n"
                                "（按从新到旧顺序依次搬回，原文件画质不受影响）"
                                ) != QMessageBox.Yes:
            return
        if self.mode == "inplace":
            root = os.path.dirname(os.path.dirname(target_file))
            # 仅在该根目录内、且只还原原位重命名产生的记录，避免与归类整理记录串台
            ok, fail, errs = undo_upto(root, target_file, kind=INPLACE_KIND)
        else:
            ok, fail, errs = undo_upto(self.folder, target_file)
        self._show_undo_result(ok, fail, errs)
        if self.mode == "inplace":
            self._after_undo_refresh()
        else:
            self._set_folder(self.folder)

    def _show_undo_result(self, ok: int, fail: int, errs: list[str]) -> None:
        """撤销结果：明细完整列出，不再只显示前 10 条。

        被跳过的文件（比如原位子已经被别的文件占了）必须让用户看到具体是哪些，
        否则他只看到一个「失败 N 个」，根本不知道该去处理谁。
        """
        if not errs:
            QMessageBox.information(self, "撤销完成", f"已还原 {ok} 个，全部成功。")
            return
        head = (f"已还原 {ok} 个，失败 {fail} 个。\n\n"
                f"下面 {len(errs)} 个没能自动还原——你的文件一个都没丢，"
                "只是需要手动搬一下（常见原因：原位子已经有同名文件了）：")
        self._detail("撤销完成（有未还原项）", head, "\n".join(errs))

    # ------------------------------------------------------------------ 表格
    def _fill(self, idx, item):
        if self.tb.rowCount() <= idx:
            self.tb.insertRow(idx)
        vals = (os.path.basename(item.src), item.category, item.reason,
                item.new_name or "", item.status + (f" · {item.detail}" if item.detail else ""))
        color = None
        if item.status == "已完成":
            color = QColor(OKC)
        elif item.status == "失败":
            color = QColor(ERRC)
        elif item.confidence and item.confidence < self.settings.min_confidence:
            color = QColor(WARNC)
        for c, s in enumerate(vals):
            it = QTableWidgetItem(str(s))
            if c in (1, 2):
                it.setTextAlignment(Qt.AlignCenter)
            if color and c in (1, 4):
                it.setForeground(color)
            self.tb.setItem(idx, c, it)

    def _refill(self):
        self.tb.setRowCount(0)
        for i, it in enumerate(self.items):
            self._fill(i, it)

    def _edit_row(self, r, _c):
        if self.busy or not (0 <= r < len(self.items)):
            return
        if self.mode == "inplace":
            self._open_loc(self.items[r].src)     # 原位模式：双击 = 打开文件所在文件夹定位
            return
        if not self.organizer:
            return
        item = self.items[r]
        names = list(dict.fromkeys([c.name for c in self.book.enabled_categories] + [self.book.fallback]))
        cur = names.index(item.category) if item.category in names else 0
        name, ok = QInputDialog.getItem(self, "改类别", os.path.basename(item.src), names, cur, False)
        if not ok or not name:
            return
        item.category = name
        item.reason = "手动指定"
        item.confidence = 1.0
        self.organizer.assign_names(self.folder, self.items)
        self._refill()

    # ------------------------------------------------------------------ 状态
    def _set_busy(self, b):
        self.busy = b
        # 按钮可用状态必须按当前模式派生：原位模式看 roots，归类模式看 folder，
        # 不能固定读 self.folder（原位模式 folder 为空，否则扫描按钮会卡在禁用态）
        self._update_scan_enabled()
        self.b_stop.setEnabled(b)
        if b:
            self.b_run.setEnabled(False)
            self.b_undo.setEnabled(False)
        else:
            self.pb.setValue(0)
            self._refresh_undo_menu()

    def _edit_cats(self):
        if self.busy:
            return
        try:
            book = CategoryBook.load()
        except Exception as e:
            QMessageBox.critical(
                self, "类别清单读不出来",
                f"{type(e).__name__}: {e}\n\n"
                "请检查程序目录下的 categories.json（手改过的话，多半是多打或漏了一个逗号）。"
            )
            return
        dlg = CategoryDialog(self, book)
        if dlg.exec() == QDialog.Accepted:
            self.book = CategoryBook.load()
            self.book.min_confidence = self.settings.min_confidence
            self._status("类别清单已更新　·　重新点「开始识别」生效", OKC)

    def closeEvent(self, e):
        try:
            if not self._sync():
                QMessageBox.warning(
                    self, "设置没存上",
                    "退出时保存界面设置失败（常见原因：程序所在目录没有写权限、磁盘已满、"
                    "被安全软件拦截）。\n\n"
                    "已经整理好的照片不受影响，只是下次启动会回到默认设置。\n"
                    "想保留设置，把整个程序文件夹放到有写权限的位置即可"
                    "（例如别放在 C:\\Program Files 下）。"
                )
        except Exception as ex:
            QMessageBox.warning(self, "退出提示", f"保存设置时出错：\n{type(ex).__name__}: {ex}")
        # 请求所有后台线程停止（协同式：执行线程循环会检查 stop_flag）
        if self.organizer:
            self.organizer.stop_flag.set()
        disk_renamer = getattr(self, "disk_renamer", None)
        if disk_renamer is not None:
            try:
                disk_renamer.stop_flag.set()
            except Exception:
                pass
        for w in (self.scan_worker, self.exec_worker,
                  self.disk_scan_worker, self.disk_exec_worker):
            if w is not None:
                w.requestInterruption()
        # 仍有线程在跑（正在搬文件 / 改名）时，先不接受关闭，
        # 等它们安全结束后再真正退出，避免「界面退了后台还在动文件」的竞态
        if any(w is not None and w.isRunning() for w in
               (self.scan_worker, self.exec_worker,
                self.disk_scan_worker, self.disk_exec_worker)):
            e.ignore()
            QTimer.singleShot(250, self.close)
            return
        e.accept()


# =========================================================================== #
def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setStyleSheet(QSS)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
