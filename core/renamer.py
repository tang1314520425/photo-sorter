# -*- coding: utf-8 -*-
"""原位重命名模式（2026-09-03）。

扫描用户勾选的磁盘根目录 → 本地 CLIP 分类 → 在「照片原位」把文件名改成
「年[.月.日].分类.序号」，不移动、不建文件夹、不碰非照片文件。

铁律与 organizer 完全一致：绝不重编码 / 降质 / 覆盖 / 篡改 / 删除。
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime

from .config import ALL_EXTS, CategoryBook, Settings
from .classifier import Classifier
from .media import probe, safe_datetime
from .organizer import (
    UNDO_DIR, _SKIP_DIRS, _looks_like_our_dir, undo_kind_of, should_skip_dir,
    sanitize, PlanItem, RunReport, Organizer, undo,
)

INPLACE_KIND = "inplace"      # 撤销记录的模式标记，与 organize 模式区分开


def _inplace_date(shot_time: float, granularity: str) -> str:
    """把时间戳转成原位重命名用的日期前缀。year→2023 / ymd→2023.08.15。

    时间戳越界（1970 年前 / 9999 年后，畸形文件名里很常见）时返回空串，
    退化成「分类.序号」，绝不让整批重命名崩掉。
    """
    if not shot_time:
        return ""
    dt = safe_datetime(shot_time)
    if dt is None:
        return ""
    if granularity == "ymd":
        return f"{dt.year}.{dt.month:02d}.{dt.day:02d}"
    return f"{dt.year}"


# 已经是我们改过的名（年[.月.日].分类.序号）-> 跳过，避免重复处理
_DONE_RE = re.compile(r"^(\d{4}(?:\.\d{2}){0,2})\.([^.]+)\.(\d{2,})$")


def scan_roots(roots: list[str], book: CategoryBook, recursive: bool = True) -> list[str]:
    """跨多个根目录收集照片文件。跳过系统目录、隐藏目录、已归类文件夹、已重命名的文件。"""
    cat_names = {n.lower() for n in book.all_folder_names()}
    found: list[str] = []
    seen: set[str] = set()
    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            rel = os.path.relpath(dirpath, root)
            # 剪枝：系统/隐藏/已生成的类别文件夹都不进
            dirnames[:] = [
                d for d in dirnames
                if not should_skip_dir(d)
                and not _looks_like_our_dir(os.path.join(dirpath, d), d, cat_names)
            ]
            if rel != "." and _looks_like_our_dir(dirpath, os.path.basename(dirpath), cat_names):
                continue
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() not in ALL_EXTS:
                    continue
                stem = os.path.splitext(fn)[0]
                m = _DONE_RE.match(stem)
                if m and m.group(2).lower() in cat_names:
                    continue                       # 已经是我们改过的名，跳过
                p = os.path.join(dirpath, fn)
                if p not in seen:
                    seen.add(p)
                    found.append(p)
    return found


class InPlaceNamer:
    """原位命名：年[.月.日].分类.序号，同一文件夹内按（日期, 分类）各自独立编号。

    两个关键点：
      1. 撞号时「递增序号」而不是追加 _1 后缀。后缀会破坏「年.分类.序号」格式，
         下次扫描认不出这是自己改过的名，就会被翻来覆去地重复处理。
      2. 目录清单只 listdir 一次并缓存。以前每个文件都 listdir 一遍，
         一个 5000 张照片的文件夹要做 2500 万次比较，光发号就得卡好几分钟。
    """

    def __init__(self, granularity: str = "year") -> None:
        self.g = granularity
        self._cache: dict[tuple, int] = {}
        self._ls: dict[str, set[str]] = {}    # 目录 → 已占用文件名（Windows 下按不区分大小写存）
        self._max: dict[tuple, int] = {}      # (目录,日期段,类别) → 已用最大序号，避免重复扫目录

    def _names_of(self, folder: str) -> set[str]:
        key = os.path.normcase(os.path.abspath(folder))
        got = self._ls.get(key)
        if got is None:
            try:
                got = {os.path.normcase(f) for f in os.listdir(folder)}
            except OSError:
                got = set()
            self._ls[key] = got
        return got

    @staticmethod
    def _taken(names: set[str], fn: str) -> bool:
        return os.path.normcase(fn) in names

    def next_name(self, folder: str, category: str, src: str, shot_time: float = 0.0):
        datekey = _inplace_date(shot_time, self.g)
        cat = sanitize(category)
        key = (folder, datekey, cat)
        cur = max(self._existing_max(folder, cat, datekey), self._cache.get(key, 0))
        # 原样保留扩展名（含大小写）。以前这里写了 .lower()，会把 IMG_0001.JPG
        # 改成 2023.风景.01.jpg —— 内容没变，但确实动了文件属性，
        # 在 Linux/macOS 上还可能被当成两个不同的文件，违反「绝不篡改」。
        ext = os.path.splitext(src)[1] or ""
        names = self._names_of(folder)
        while True:
            cur += 1
            seq = f"{cur:02d}"
            stem = f"{datekey}.{cat}.{seq}" if datekey else f"{cat}.{seq}"
            fn = stem + ext
            if not self._taken(names, fn):
                break
        names.add(os.path.normcase(fn))      # 本次已占用，同批次后续不再撞
        self._cache[key] = max(self._cache.get(key, 0), cur)
        return os.path.join(folder, fn), fn

    def _existing_max(self, folder: str, cat: str, datekey: str) -> int:
        """扫描目录里已有的最大序号。结果按 (目录,日期段,类别) 缓存，
        否则每发一个号都要把整个目录重新滤一遍，一千张照片就是一百万次比较。"""
        ck = (os.path.normcase(os.path.abspath(folder)), datekey, cat)
        cached = self._max.get(ck)
        if cached is not None:
            return cached
        pat = (re.compile(re.escape(f"{datekey}.{cat}.") + r"(\d{2,})$") if datekey
               else re.compile(re.escape(f"{cat}.") + r"(\d{2,})$"))
        m = 0
        for fn in self._names_of(folder):     # 用缓存清单，不再每次都 listdir
            mm = pat.match(os.path.splitext(fn)[0])
            if mm:
                try:
                    m = max(m, int(mm.group(1)))
                except ValueError:
                    pass
        self._max[ck] = m
        return m


class InPlaceRenamer:
    def __init__(self, book: CategoryBook, settings: Settings, classifier: Classifier) -> None:
        self.book = book
        self.settings = settings
        self.classifier = classifier
        self.roots: list[str] = []
        self.stop_flag = threading.Event()

    # ---------- 第一步：识别 ----------
    def build_plan(self, roots: list[str], progress=None) -> list[PlanItem]:
        self.roots = [os.path.abspath(r) for r in roots]
        files = scan_roots(self.roots, self.book)
        items: list[PlanItem] = []
        total = len(files)
        for i, path in enumerate(files, 1):
            if self.stop_flag.is_set():
                break
            info = probe(path, date_source=self.settings.date_source)
            res = self.classifier.classify(
                info, video_own=self.settings.video_as_own_category,
                min_conf=self.settings.min_confidence,
            )
            items.append(PlanItem(
                src=path, category=sanitize(res.category),
                confidence=res.confidence, reason=res.reason,
                shot_time=info.shot_time, time_source=info.time_source, detail=info.error,
            ))
            if progress:
                progress(i, total, path, items[-1])
            info.thumb = None
            info.extra_frames = []
        return items

    # ---------- 第二步：编号（在原位）----------
    def assign_names(self, items: list[PlanItem]) -> None:
        namer = InPlaceNamer(self.settings.inplace_date_granularity)
        # 同文件夹、同类别、同年内按拍摄时间排序，编号顺序符合直觉
        order = sorted(
            range(len(items)),
            key=lambda i: (os.path.dirname(items[i].src), items[i].category,
                           items[i].shot_time, items[i].src),
        )
        for i in order:
            it = items[i]
            it.dst, it.new_name = namer.next_name(
                os.path.dirname(it.src), it.category, it.src, it.shot_time)

    # ---------- 第三步：落地（原地改名）----------
    def execute(self, items: list[PlanItem], progress=None) -> RunReport:
        rep = RunReport(total=len(items), items=items)
        moves_by_root: dict[str, list[dict]] = {}
        for i, it in enumerate(items, 1):
            if self.stop_flag.is_set():
                break
            try:
                if Organizer._same_file(it.src, it.dst):
                    it.status, it.detail = "跳过", "已在该名"
                    rep.skipped += 1
                else:
                    Organizer._safe_move(it.src, it.dst)   # 同文件夹即 os.rename，零拷贝零重编码
                    it.status = "已完成"
                    rep.moved += 1
                    rep.by_category[it.category] = rep.by_category.get(it.category, 0) + 1
                    root = self._root_of(it.src)
                    moves_by_root.setdefault(root, []).append({"src": it.src, "dst": it.dst})
            except Exception as e:
                it.status, it.detail = "失败", f"{type(e).__name__}: {e}"
                rep.failed += 1
            if progress:
                progress(i, len(items), it.src, it)
        # 每个根目录各自写一份撤销记录（沿用 organizer 的机制）
        for root, moves in moves_by_root.items():
            if moves:
                try:
                    p = self._write_undo(root, moves, kind=INPLACE_KIND)
                    if not rep.undo_file:
                        rep.undo_file = p
                except Exception as e:
                    rep.warning = (rep.warning + "\n" if rep.warning else "") + \
                        f"撤销记录写入失败（{os.path.basename(root)}）：{type(e).__name__}: {e}"
        return rep

    def _root_of(self, path: str) -> str:
        ap = os.path.abspath(path)
        for r in self.roots:
            if ap == r or ap.startswith(r + os.sep):
                return r
        # 兜底：写进文件所在的那个目录（而不是祖父目录）。
        # 以前返回 dirname(dirname)，会把撤销记录扔到莫名其妙的上层目录去。
        return os.path.dirname(ap)

    @staticmethod
    def _write_undo(root: str, log: list[dict], kind: str = INPLACE_KIND) -> str:
        from .organizer import Organizer
        return Organizer._write_undo(root, log, kind=kind)


# --------------------------------------------------------------------------- #
# 撤销（跨多个根目录）
# --------------------------------------------------------------------------- #
def list_disk_undo_files(roots: list[str], kind: str | None = INPLACE_KIND) -> list[str]:
    """列出所有扫描根目录下的撤销记录（新→旧）。

    kind 默认只取「原位重命名」产生的记录。
    这样做的原因：同一个根目录可能以前用过「归类整理」模式，那边也留着撤销记录。
    若不分模式，用户在原位模式点一下撤销，就会把当年归类整理的那次移动一起撤掉 —— 串台了。
    """
    files: list[str] = []
    for r in roots:
        r = os.path.abspath(r)
        d = os.path.join(r, UNDO_DIR)
        if os.path.isdir(d):
            fs = [os.path.join(d, f) for f in os.listdir(d)
                  if f.startswith("undo_") and f.endswith(".json")]
            if kind:
                fs = [p for p in fs if undo_kind_of(p) == kind]
            files.extend(fs)
    return sorted(files, reverse=True)


def undo_disk(roots: list[str], progress=None,
              kind: str | None = INPLACE_KIND) -> tuple[int, int, list[str]]:
    """撤销整次磁盘扫描产生的所有重命名（逐根目录还原，严格不覆盖）。"""
    files = list_disk_undo_files(roots, kind=kind)
    ok = fail = 0
    errs: list[str] = []
    for i, f in enumerate(files, 1):
        o, f2, e = undo(f)
        ok += o
        fail += f2
        errs.extend(e)
        if progress:
            progress(i, len(files))
    return ok, fail, errs
