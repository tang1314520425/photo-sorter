# -*- coding: utf-8 -*-
"""扫描 → 规划 → 执行 → 可撤销。

四条硬约束，写死在代码里：
  1. 绝不重编码：只做文件系统层面的移动/改名，一个字节都不会重写。
  2. 绝不降画质：读取时的缩放全部发生在内存副本上，原文件不受影响。
  3. 绝不覆盖：目标存在就自动换序号，并且用 os.rename（Windows 下目标已存在会直接报错）双保险。
  4. 绝不篡改：不写 EXIF、不改时间戳、不动扩展名。
"""

from __future__ import annotations

import json
import os
import re
import errno
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from .config import ALL_EXTS, CategoryBook, Settings
from .classifier import Classifier
from .media import probe

UNDO_DIR = ".photo_sorter_undo"
_SKIP_DIRS = {UNDO_DIR, "$RECYCLE.BIN", "System Volume Information", ".git", "__pycache__"}


@dataclass
class PlanItem:
    src: str
    category: str
    confidence: float
    reason: str
    dst: str = ""
    new_name: str = ""
    status: str = "待处理"        # 待处理 / 已完成 / 跳过 / 失败
    detail: str = ""
    shot_time: float = 0.0


@dataclass
class RunReport:
    total: int = 0
    moved: int = 0
    skipped: int = 0
    failed: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    undo_file: str = ""
    warning: str = ""        # 移动成功但撤销记录写入失败等需要提示用户的非致命信息
    items: list[PlanItem] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #
def scan_folder(root: str, book: CategoryBook, recursive: bool = False) -> list[str]:
    """收集待整理的媒体文件。已经分好类的类别文件夹会自动跳过，不会被重复搬运。"""
    root = os.path.abspath(root)
    cat_names = {n.lower() for n in book.all_folder_names()}
    found: list[str] = []

    if not recursive:
        try:
            entries = os.scandir(root)
        except OSError:
            return []
        with entries:
            for e in entries:
                if e.is_file() and os.path.splitext(e.name)[1].lower() in ALL_EXTS:
                    found.append(e.path)
        return found

    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        # 剪枝：已生成的类别文件夹（含「时间-类别」格式）、隐藏目录、系统目录都不进
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
            and not _looks_like_our_dir(os.path.join(dirpath, d), d, cat_names)
        ]
        if rel != "." and _looks_like_our_dir(dirpath, os.path.basename(dirpath), cat_names):
            continue
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in ALL_EXTS:
                found.append(os.path.join(dirpath, fn))
    return found


# --------------------------------------------------------------------------- #
# 命名
# --------------------------------------------------------------------------- #
_INVALID = re.compile(r'[\\/:*?"<>|]')


def _looks_like_our_dir(dirpath: str, name: str, cat_names: set[str]) -> bool:
    """判断 dirpath 是不是程序自己生成的类别文件夹（避免二次运行时重复搬运）。

    命中规则：目录名 == 某类别名，或 以「-类别名」结尾（即「时间-类别」格式）。
    但仅按名字还不够稳——用户完全可能有一个叫「旅行」/「2023-人像」的真实文件夹。
    所以再加一道闸：目录里必须确实存在形如「类别NN」的文件，才认定是程序产物。
    """
    n = (name or "").strip().lower()
    if not n:
        return False
    base = n if n in cat_names else None
    if base is None:
        for c in cat_names:
            if n.endswith("-" + c):
                base = c
                break
    if base is None:
        return False
    try:
        for fn in os.listdir(dirpath):
            if os.path.isfile(os.path.join(dirpath, fn)):
                stem = os.path.splitext(fn)[0]
                if re.match(r"^" + re.escape(base) + r"\d{2,}", stem):
                    return True
    except OSError:
        return False
    return False


# --------------------------------------------------------------------------- #
# 时间 → 命名片段
# --------------------------------------------------------------------------- #
def date_segment(ts: float, granularity: str) -> str:
    """把时间戳转成编号用的日期段（自动取照片创建时间）。

    year  -> 2023
    month -> 2023-05
    day   -> 2023-05-12
    取不到时间（ts=0）时返回空串，退化成纯序号。
    """
    if not ts:
        return ""
    dt = datetime.fromtimestamp(ts)
    if granularity == "year":
        return f"{dt.year}"
    if granularity == "month":
        return f"{dt.year}-{dt.month:02d}"
    return f"{dt.year}-{dt.month:02d}-{dt.day:02d}"


def sanitize(name: str) -> str:
    return _INVALID.sub("_", name).strip().strip(".") or "未命名"


class Namer:
    """按「类别 + 日期段(可选) + 两位序号」发号，续接目标文件夹里已有的编号。

    两种文件夹布局（由 time_block 决定）：
      · 不勾时间板块：    root/{类别}/           例：root/人像/
      · 勾选时间板块：    root/{时间}-{类别}/    例：root/2021-人像/
    文件名：
      · 时间已在文件夹里（勾了时间板块）→ 不再重复日期，{类别}{序号}.jpg
      · 否则带日期段（手动或自动取创建时间），{类别}{日期段}-{序号}.jpg
        日期段留空则退化为 {类别}{序号}.jpg
    """

    def __init__(self, root: str, date_tag: str, keep_original: bool = False,
                 date_numbering: bool = False, date_granularity: str = "day",
                 time_block: bool = False, time_block_granularity: str = "year") -> None:
        self.root = root
        self.date_tag = sanitize(date_tag) if (date_tag or "").strip() else ""
        self.keep_original = keep_original
        self.date_numbering = date_numbering
        self.date_granularity = date_granularity
        self.time_block = time_block
        self.time_block_granularity = time_block_granularity
        # 按「最终文件夹路径」缓存（不同年份 / 不同类别各自独立续号）
        self._cache: dict[str, dict] = {}

    def _time_tag(self, shot_time: float) -> str:
        if self.date_numbering:
            return date_segment(shot_time, self.date_granularity)   # 自动取创建时间
        return self.date_tag                                       # 手动日期段

    def _scan_folder(self, folder: str, category: str) -> dict:
        taken: set[str] = set()
        start = 0
        pat = re.compile(r"^" + re.escape(category)
                         + r"(?:-\d{2,4}(?:-\d{2}){0,2})?-?(\d{2,})(?:_.*)?$")
        stack = [folder]
        while stack:
            d = stack.pop()
            if not os.path.isdir(d):
                continue
            try:
                for fn in os.listdir(d):
                    fp = os.path.join(d, fn)
                    if os.path.isdir(fp):
                        stack.append(fp)          # 嵌套目录（如旧的时间板块）也统计
                    else:
                        taken.add(fp.lower())     # 用完整目标路径去重
                        m = pat.match(os.path.splitext(fn)[0])
                        if m:
                            start = max(start, int(m.group(1)))
            except OSError:
                pass
        return {"max": start, "taken": taken}

    def next_name(self, category: str, src: str, shot_time: float = 0.0) -> tuple[str, str]:
        tt = self._time_tag(shot_time)

        # —— 文件夹名 ——
        if self.time_block and tt:
            folder_name = f"{tt}-{category}"      # 2021-人像
        else:
            folder_name = category                # 人像
        folder = os.path.join(self.root, folder_name)
        if folder not in self._cache:
            self._cache[folder] = self._scan_folder(folder, category)
        state = self._cache[folder]

        # —— 文件名里的日期段 ——
        # 勾了时间板块时，时间已体现在文件夹名里，文件名不再重复带日期
        fn_date = "" if (self.time_block and tt) else tt

        base_old, ext = os.path.splitext(os.path.basename(src))
        while True:
            state["max"] += 1
            seq = f"{state['max']:02d}"           # 超过 99 自动变三位、四位，不会撞号
            if fn_date:
                stem = f"{category}{fn_date}-{seq}"
            else:
                stem = f"{category}{seq}"         # 人像01 / 风景02
            if self.keep_original:
                stem = f"{stem}_{sanitize(base_old)[:40]}"
            fn = stem + ext
            dst = os.path.join(folder, fn)
            if dst.lower() in state["taken"]:
                continue
            if os.path.exists(dst):              # 双保险：文件系统层面再确认一次
                state["taken"].add(dst.lower())
                continue
            state["taken"].add(dst.lower())
            return dst, fn


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
class Organizer:
    def __init__(self, book: CategoryBook, settings: Settings, classifier: Classifier) -> None:
        self.book = book
        self.settings = settings
        self.classifier = classifier
        self.stop_flag = threading.Event()

    # ---------- 第一步：识别，产出计划 ----------
    def build_plan(self, root: str, files: list[str], progress=None) -> list[PlanItem]:
        items: list[PlanItem] = []
        total = len(files)
        for i, path in enumerate(files, 1):
            if self.stop_flag.is_set():
                break
            info = probe(path)
            res = self.classifier.classify(
                info,
                video_own=self.settings.video_as_own_category,
                min_conf=self.settings.min_confidence,
            )
            items.append(
                PlanItem(
                    src=path,
                    category=sanitize(res.category),
                    confidence=res.confidence,
                    reason=res.reason,
                    shot_time=info.shot_time,
                    detail=info.error,
                )
            )
            if progress:
                progress(i, total, path, items[-1])
            info.thumb = None
            info.extra_frames = []
        return items

    # ---------- 第二步：编号 ----------
    def assign_names(self, root: str, items: list[PlanItem]) -> None:
        namer = Namer(
            root,
            self.settings.date_tag,
            self.settings.keep_original_name,
            date_numbering=self.settings.date_numbering,
            date_granularity=self.settings.date_granularity,
            time_block=self.settings.time_block,
            time_block_granularity=self.settings.time_block_granularity,
        )
        # 同类内按拍摄时间排序，编号顺序才符合直觉
        order = sorted(range(len(items)), key=lambda i: (items[i].category, items[i].shot_time, items[i].src))
        for i in order:
            it = items[i]
            it.dst, it.new_name = namer.next_name(it.category, it.src, it.shot_time)

    # ---------- 第三步：落地 ----------
    def execute(self, root: str, items: list[PlanItem], progress=None) -> RunReport:
        rep = RunReport(total=len(items), items=items)
        moved_log: list[dict] = []

        for i, it in enumerate(items, 1):
            if self.stop_flag.is_set():
                break
            try:
                if os.path.abspath(it.src) == os.path.abspath(it.dst):
                    it.status, it.detail = "跳过", "已在目标位置"
                    rep.skipped += 1
                else:
                    folder = os.path.dirname(it.dst)
                    os.makedirs(folder, exist_ok=True)
                    self._safe_move(it.src, it.dst)
                    it.status = "已完成"
                    rep.moved += 1
                    rep.by_category[it.category] = rep.by_category.get(it.category, 0) + 1
                    moved_log.append({"src": it.src, "dst": it.dst})
            except Exception as e:
                it.status, it.detail = "失败", f"{type(e).__name__}: {e}"
                rep.failed += 1
            if progress:
                progress(i, len(items), it.src, it)

        if moved_log:
            try:
                rep.undo_file = self._write_undo(root, moved_log)
            except Exception as e:
                # 文件已移动成功，但撤销记录写不进去（只读介质/磁盘满等）。
                # 不能让界面误报「出错」，文件也没丢，只是这次无法一键撤销。
                rep.undo_file = ""
                rep.warning = (
                    f"已成功移动 {len(moved_log)} 个文件，但撤销记录写入失败"
                    f"（{type(e).__name__}: {e}）。文件未丢失，本次无法一键撤销，建议先手动备份。"
                )
        return rep

    @staticmethod
    def _safe_move(src: str, dst: str) -> None:
        """只做移动，不做任何内容层面的操作。目标已存在 → 直接抛错，绝不覆盖。

        同盘用 os.rename（原子、零拷贝、零重编码）；跨盘才降级为 copy2+校验+删源。
        关键安全约束：除「跨设备」错误外，任何其它错误（权限/占用/竞态产生的
        文件已存在）一律向上抛，绝不落到 copy2 覆盖分支，守住「绝不覆盖」铁律。
        """
        if os.path.exists(dst):
            raise FileExistsError(f"目标已存在，拒绝覆盖：{dst}")
        try:
            os.rename(src, dst)          # 同盘：原子操作，零拷贝、零重编码
        except FileExistsError:
            # 竞态窗口（检查存在 → rename 之间目标被别的进程创建）一律失败，
            # 绝不进入下面的 copy2 覆盖分支。
            raise
        except OSError as e:
            # 只有「跨设备」错误才走字节级复制回退；其余错误直接失败。
            is_cross = (e.errno == errno.EXDEV
                        or getattr(e, "winerror", None) in (17,))   # 17 = 无法移到不同磁盘
            if not is_cross:
                raise
            # 跨盘：字节级复制 + 保留时间戳，仍然不改内容
            shutil.copy2(src, dst)
            if os.path.getsize(src) != os.path.getsize(dst):
                try:
                    os.remove(dst)
                except OSError:
                    pass
                raise IOError("复制后大小不一致，已回滚")
            os.remove(src)

    @staticmethod
    def _write_undo(root: str, log: list[dict]) -> str:
        d = os.path.join(root, UNDO_DIR)
        os.makedirs(d, exist_ok=True)
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(d, 0x02)   # 隐藏这个记录文件夹
        except Exception:
            pass
        # 同一秒内连续归档也要各存一份，绝不互相覆盖，否则撤销会丢记录
        stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
        p = os.path.join(d, f"undo_{stamp}.json")
        n = 1
        while os.path.exists(p):
            p = os.path.join(d, f"undo_{stamp}_{n:02d}.json")
            n += 1
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"time": time.time(), "root": root, "moves": log}, f, ensure_ascii=False, indent=1)
        return p


# --------------------------------------------------------------------------- #
# 撤销
# --------------------------------------------------------------------------- #
def list_undo_files(root: str) -> list[str]:
    d = os.path.join(root, UNDO_DIR)
    if not os.path.isdir(d):
        return []
    fs = [os.path.join(d, f) for f in os.listdir(d) if f.startswith("undo_") and f.endswith(".json")]
    return sorted(fs, reverse=True)


def undo(undo_file: str, progress=None) -> tuple[int, int, list[str]]:
    """把文件原样搬回去。同样绝不覆盖。"""
    with open(undo_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    moves = data.get("moves", [])
    ok = fail = 0
    errs: list[str] = []
    for i, m in enumerate(reversed(moves), 1):
        src, dst = m["src"], m["dst"]
        try:
            if not os.path.exists(dst):
                fail += 1
                errs.append(f"已不在原处：{os.path.basename(dst)}")
                continue
            if os.path.exists(src):
                fail += 1
                errs.append(f"原位置已被占用，跳过：{os.path.basename(src)}")
                continue
            os.makedirs(os.path.dirname(src), exist_ok=True)
            Organizer._safe_move(dst, src)   # 对称使用安全移动，跨盘也能正确撤销
            ok += 1
        except Exception as e:
            fail += 1
            errs.append(f"{os.path.basename(dst)}: {e}")
        if progress:
            progress(i, len(moves))
    try:
        os.replace(undo_file, undo_file + ".done")
    except Exception:
        pass
    return ok, fail, errs
