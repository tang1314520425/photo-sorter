# -*- coding: utf-8 -*-
"""照片 / 视频智能分类整理程序  ——  拖进来，自动分类，自动编号。

全程离线运行：不上传任何文件，不调用任何云端接口，不消耗任何积分。
"""

from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt, QThread, Signal, QSize
from PySide6.QtGui import QFont, QColor, QIcon, QPixmap, QPainter, QBrush
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QCheckBox, QSlider, QFrame, QTableWidget,
    QTableWidgetItem, QProgressBar, QFileDialog, QMessageBox, QDialog,
    QListWidget, QComboBox, QPlainTextEdit, QAbstractItemView, QHeaderView,
    QInputDialog, QSizePolicy,
)

from core.config import CategoryBook, Category, Settings, ALL_EXTS
from core.classifier import Classifier, SemanticEngine
from core.organizer import Organizer, scan_folder, list_undo_files, undo

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
            clf = Classifier(self.book, use_ai=True)
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

    def _flush(self):
        if not (0 <= self.cur < len(self.book.categories)):
            return
        c = self.book.categories[self.cur]
        n = self.ed_name.text().strip()
        if n:
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
        self.book = CategoryBook.load()
        self.settings = Settings.load()
        self.folder = ""
        self.items: list = []
        self._item_index: dict[int, int] = {}
        self.scan_worker: ScanWorker | None = None
        self.exec_worker: ExecWorker | None = None
        self.organizer: Organizer | None = None
        self.busy = False

        self.setWindowTitle(APP_TITLE)
        self.resize(1120, 740)
        self.setMinimumSize(940, 620)
        self.setStyleSheet(QSS)
        self._build()
        self._engine_hint()

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
        self.b_undo = QPushButton("撤销上次整理")
        self.b_undo.setObjectName("danger")
        self.b_undo.setEnabled(False)
        self.b_undo.clicked.connect(self._undo)
        a.addWidget(self.b_undo)
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
        self._sample()

    # ------------------------------------------------------------------ 小工具
    def _engine_hint(self):
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

    def _sample(self):
        keep = self.cb_keep.isChecked()
        block = self.cb_block.isChecked()
        # 时间标签来源：期日编号（自动取创建时间）或 手动日期段
        if self.cb_date.isChecked():
            g = self.cmb_date_g.currentData()
            tt = {"year": "2023", "month": "2023-05", "day": "2023-05-12"}.get(g, "2023")
        else:
            tt = self.ed_date.text().strip()

        # 文件名：勾时间板块时时间已体现在文件夹，文件名不再重复带日期
        if block and tt:
            fname = "人像01"
        elif tt:
            fname = f"人像{tt}-01"
        else:
            fname = "人像01"
        if keep:
            fname += "_IMG_2233"

        # 文件夹：人像/  或  时间-人像/
        if block and tt:
            folder = f"{tt}-人像/"
        elif block:
            folder = "人像/  ⚠时间板块需填「日期段」或勾「期日编号」"
        else:
            folder = "人像/"

        self.lb_sample.setText(f"示例：{folder}{fname}.jpg")

    def _status(self, t, color=TEXT):
        self.lb_status.setText(t)
        self.lb_status.setStyleSheet(f"color:{color};")

    def _sync(self):
        self.settings.date_tag = self.ed_date.text().strip()
        self.settings.recursive = self.cb_rec.isChecked()
        self.settings.video_as_own_category = self.cb_vid.isChecked()
        self.settings.keep_original_name = self.cb_keep.isChecked()
        self.settings.min_confidence = self.sl.value() / 100.0
        self.settings.date_numbering = self.cb_date.isChecked()
        self.settings.date_granularity = self.cmb_date_g.currentData()
        self.settings.time_block = self.cb_block.isChecked()
        self.settings.time_block_granularity = self.cmb_block_g.currentData()
        self.settings.last_folder = self.folder
        self.book.min_confidence = self.settings.min_confidence
        self.settings.save()

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
        self.tb.setRowCount(0)
        self.items = []
        self._status("已选中文件夹　·　先点「开始识别」看结果，确认无误再归档")

    # ------------------------------------------------------------------ 识别
    def _start_scan(self):
        if self.busy or not self.folder:
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
        self.b_run.setEnabled(True)

    # ------------------------------------------------------------------ 执行
    def _start_exec(self):
        if self.busy or not self.items or not self.organizer:
            return
        todo = [i for i in self.items if i.status == "待处理"]
        if not todo:
            QMessageBox.information(self, "无事可做", "没有待处理的文件。")
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
        self.b_undo.setEnabled(bool(rep.undo_file))
        self._status(f"归档完成：成功 {rep.moved}，跳过 {rep.skipped}，失败 {rep.failed}", OKC)
        lines = "\n".join(f"　{k} → {v} 个" for k, v in sorted(rep.by_category.items(), key=lambda x: -x[1]))
        detail = (
            f"成功 {rep.moved} 个，跳过 {rep.skipped} 个，失败 {rep.failed} 个。\n\n{lines}\n\n"
            "原文件画质、格式、拍摄信息全部原样保留。\n不满意可点「撤销上次整理」还原。"
        )
        if rep.warning:
            detail += "\n\n⚠ " + rep.warning
            self._status("归档完成 · 注意：撤销记录写入失败（详见弹窗）", WARNC)
        QMessageBox.information(self, "整理完成", detail)

    def _on_fail(self, tb):
        self._set_busy(False)
        self._status("出错了", ERRC)
        QMessageBox.critical(self, "出错", tb[-1800:])

    def _stop(self):
        if self.organizer:
            self.organizer.stop_flag.set()
        if self.scan_worker and self.scan_worker.organizer:
            self.scan_worker.organizer.stop_flag.set()
        self._status("正在停止…", WARNC)

    # ------------------------------------------------------------------ 撤销
    def _undo(self):
        files = list_undo_files(self.folder)
        if not files:
            QMessageBox.information(self, "没有记录", "这个文件夹没有可撤销的整理记录。")
            return
        latest = files[0]
        if QMessageBox.question(self, "撤销",
                                f"把上一次整理的文件全部搬回原位？\n\n记录：{os.path.basename(latest)}"
                                ) != QMessageBox.Yes:
            return
        ok, fail, errs = undo(latest)
        tip = f"已还原 {ok} 个，失败 {fail} 个。"
        if errs:
            tip += "\n\n" + "\n".join(errs[:10])
        QMessageBox.information(self, "撤销完成", tip)
        self._set_folder(self.folder)

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
        if self.busy or not (0 <= r < len(self.items)) or not self.organizer:
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
        self.b_scan.setEnabled(not b and bool(self.folder))
        self.b_stop.setEnabled(b)
        if b:
            self.b_run.setEnabled(False)
            self.b_undo.setEnabled(False)
        else:
            self.pb.setValue(0)
            self.b_undo.setEnabled(bool(self.folder) and bool(list_undo_files(self.folder)))

    def _edit_cats(self):
        if self.busy:
            return
        dlg = CategoryDialog(self, CategoryBook.load())
        if dlg.exec() == QDialog.Accepted:
            self.book = CategoryBook.load()
            self.book.min_confidence = self.settings.min_confidence
            self._status("类别清单已更新　·　重新点「开始识别」生效", OKC)

    def closeEvent(self, e):
        try:
            self._sync()
        except Exception:
            pass
        if self.organizer:
            self.organizer.stop_flag.set()
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
