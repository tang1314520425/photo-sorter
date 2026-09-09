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
from .media import probe, safe_datetime

UNDO_DIR = ".photo_sorter_undo"
MARKER_FILE = ".photo_sorter_marker"   # 程序自建类别文件夹的隐藏标记，供 _looks_like_our_dir 可靠识别

# 扫描时必须跳过的目录。
# 前 5 个是原有的（程序自己的撤销目录 + 回收站 + 系统卷信息 + 版本控制 + 缓存）；
# 后面这些是补上的：用户可能直接把 C:\ 或 D:\ 整个盘当作扫描根目录，
# 不剪枝就会一头扎进 Windows\WinSxS、Program Files、AppData、node_modules，
# 几十万文件扫到天荒地老，还会撞权限错误和杀软告警。
_SKIP_DIRS = {
    UNDO_DIR, "$RECYCLE.BIN", "System Volume Information", ".git", "__pycache__",
    # Windows 系统目录
    "Windows", "System32", "SysWOW64", "WinSxS", "ProgramData",
    "Program Files", "Program Files (x86)", "AppData", "Recovery",
    "$Windows.~BT", "$Windows.~WS", "PerfLogs", "Programme",
    # 开发依赖 / 缓存目录（照片基本不可能在这里）
    "node_modules", ".venv", "venv", "site-packages", ".gradle", ".m2",
    ".cache", ".npm", ".conda", "dist-info", "__MACOSX",
}

# 盘符根目录扫描时的额外告警名单：命中就提示用户「这个目录不适合整盘扫描」
_WARN_ROOTS = {"Windows", "Program Files", "Program Files (x86)",
               "ProgramData", "AppData", "System32"}

# 小写副本：Windows 目录名大小写不固定（回收站实际叫 $Recycle.Bin，
# 而清单里写的是 $RECYCLE.BIN），按原名精确比对会漏掉，剪枝形同虚设。
_SKIP_DIRS_LC = {s.lower() for s in _SKIP_DIRS}


def should_skip_dir(name: str) -> bool:
    """目录剪枝判断（大小写不敏感）。"""
    return name.lower() in _SKIP_DIRS_LC or name.startswith(".")


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
    time_source: str = ""     # exif / filename / filetime / ctime / video_meta / unknown


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
            if not should_skip_dir(d)
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
_INVALID = re.compile(r'[\\/:*?"<>|\x00-\x1f]')   # Windows 非法字符，补上控制字符 0x00-0x1F
# Windows 保留设备名：单独拿来做文件名会创建失败（CON.txt 也算，所以只判断主干部分）
_RESERVED = ({"CON", "PRN", "AUX", "NUL"}
             | {f"COM{i}" for i in range(1, 10)}
             | {f"LPT{i}" for i in range(1, 10)})


def _looks_like_our_dir(dirpath: str, name: str, cat_names: set[str]) -> bool:
    """判断 dirpath 是不是程序自己生成的类别文件夹（避免二次运行时重复搬运）。

    命中规则：目录名 == 某类别名，或 以「-类别名」结尾（即「时间-类别」格式）。
    但仅按名字还不够稳——用户完全可能有一个叫「旅行」/「2023-人像」的真实文件夹。
    所以再加一道闸：目录里必须确实存在形如「类别NN」的文件，才认定是程序产物。
    """
    n = (name or "").strip().lower()
    if not n:
        return False

    # 最可靠的依据：程序生成类别文件夹时会放一个隐藏标记文件。
    # 只在「目录名也确实匹配某个类别」时才采信，避免误伤用户自建的同名目录。
    if os.path.exists(os.path.join(dirpath, MARKER_FILE)):
        return n in cat_names or any(n == c or n.endswith(c) for c in cat_names)

    base = n if n in cat_names else None
    if base is None:
        for c in cat_names:
            if n == c or n.endswith(c):   # 2026人像 / 2026-08人像 / 2023-05-12-人像 都算
                base = c
                break
    if base is None:
        return False
    try:
        for fn in os.listdir(dirpath):
            if os.path.isfile(os.path.join(dirpath, fn)):
                stem = os.path.splitext(fn)[0]
                if re.match(r"^" + re.escape(base) + r"\d{2,}", stem, re.IGNORECASE):
                    return True
    except OSError:
        return False
    return False


def _set_hidden(path: str) -> None:
    """把文件/文件夹设成隐藏（仅 Windows 有效，其它系统静默跳过）。"""
    try:
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(path, 0x02)
    except Exception:
        pass


def _mark_our_dir(folder: str) -> None:
    """在程序自己建的类别文件夹里放一个隐藏标记，供下次扫描可靠识别。

    为什么需要它：只靠「目录名 + 文件名格式」猜，会把用户自建的同名文件夹
    （比如叫「2023人像」的旅行相册，里面恰好有「人像01.jpg」）误判成程序产物，
    整个目录被跳过，照片就漏整理了。
    """
    try:
        p = os.path.join(folder, MARKER_FILE)
        if os.path.exists(p):
            return
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"由「照片/视频智能分类整理」生成于 {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        _set_hidden(p)
    except OSError:
        pass      # 标记写不进去也不影响整理，下次退回按名字+文件名格式判断


def _cleanup_empty_dirs(dirpath: str) -> None:
    """撤销后：只有标记、且已被搬空的目录，删掉它，尽量回到整理前的样子。

    绝不碰用户自己的目录——没有标记，或里面还有任何别的文件，一律不动。
    """
    try:
        if not os.path.isdir(dirpath):
            return
        marker = os.path.join(dirpath, MARKER_FILE)
        if not os.path.exists(marker):
            return
        if [e.name for e in os.scandir(dirpath) if e.name != MARKER_FILE]:
            return                       # 还有别的文件，不动
        os.remove(marker)
        os.rmdir(dirpath)
    except OSError:
        pass


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
    dt = safe_datetime(ts)
    if dt is None:                 # 时间戳越界（1970 年前 / 9999 年后）→ 退化成不带日期，绝不让整批崩
        return ""
    if granularity == "year":
        return f"{dt.year}"
    if granularity == "month":
        return f"{dt.year}-{dt.month:02d}"
    return f"{dt.year}-{dt.month:02d}-{dt.day:02d}"


def sanitize(name: str) -> str:
    s = _INVALID.sub("_", name).strip().strip(".") or "未命名"
    if s.split(".")[0].upper() in _RESERVED:
        s = "_" + s          # 撞上 Windows 保留设备名，加个下划线就能正常创建
    return s


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
        """扫描已存在的编号，按「文件夹+日期段」分组返回各自最大序号。

        这样同一文件夹内不同日期的照片各自从 01 开始编号，不会跨月串号。
        """
        seqs: dict[str, int] = {}
        pat = re.compile(r"^" + re.escape(category)
                         + r"(?:-(\d{4}(?:-\d{2}){0,2}))?-?(\d{2,})(?:_.*)?$")
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
                        continue
                    m = pat.match(os.path.splitext(fn)[0])
                    if m:
                        fd = m.group(1) or ""     # 日期段（可能为空）
                        key = f"{folder}|{fd}"
                        seqs[key] = max(seqs.get(key, 0), int(m.group(2)))
            except OSError:
                pass
        return seqs

    def next_name(self, category: str, src: str, shot_time: float = 0.0) -> tuple[str, str]:
        # 文件名日期段：手动日期段或按 date_granularity 自动取（始终带在文件名上）
        file_tag = self._time_tag(shot_time)

        # —— 文件夹名 ——
        if self.time_block:
            # 时间板块：文件夹按 time_block_granularity（年/月）分，文件名仍带完整日期
            tb = date_segment(shot_time, self.time_block_granularity) if shot_time else ""
            folder_name = f"{tb}{category}" if tb else category   # 2026人像 / 2026-08人像
        else:
            folder_name = category                # 人像
        folder = os.path.join(self.root, folder_name)
        if folder not in self._cache:
            self._cache[folder] = self._scan_folder(folder, category)
        state = self._cache[folder]
        key = f"{folder}|{file_tag}"   # 同一文件夹内，不同日期段各自独立编号

        base_old, ext = os.path.splitext(os.path.basename(src))
        while True:
            cur = state.get(key, 0) + 1
            seq = f"{cur:02d}"           # 超过 99 自动变三位、四位，不会撞号
            if file_tag:
                stem = f"{category}{file_tag}-{seq}"
            else:
                stem = f"{category}{seq}"         # 人像01 / 风景02
            if self.keep_original:
                stem = f"{stem}_{sanitize(base_old)[:40]}"
            fn = stem + ext
            dst = os.path.join(folder, fn)
            if os.path.exists(dst):              # 双保险：文件系统层面再确认一次
                state[key] = max(state.get(key, 0), cur)
                continue
            state[key] = cur
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
            info = probe(path, date_source=self.settings.date_source)
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
                    time_source=info.time_source,
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
                if Organizer._same_file(it.src, it.dst):
                    it.status, it.detail = "跳过", "已在目标位置"
                    rep.skipped += 1
                else:
                    folder = os.path.dirname(it.dst)
                    # 只给「这次新建出来」的文件夹打标记，绝不往用户已有的目录里塞东西
                    fresh = not os.path.isdir(folder)
                    os.makedirs(folder, exist_ok=True)
                    if fresh:
                        _mark_our_dir(folder)
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
    def _same_file(a: str, b: str) -> bool:
        """Windows 文件系统不区分大小写，'A.jpg' 和 'a.jpg' 是同一个文件。

        只比字符串会把它当成「目标已存在」而拒绝，
        结果一个只想改改文件名大小写的操作被误判成覆盖风险。
        """
        try:
            return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
        except Exception:
            return a == b

    @staticmethod
    def _safe_move(src: str, dst: str) -> None:
        """只做移动，不做任何内容层面的操作。目标已存在 → 直接抛错，绝不覆盖。

        同盘用 os.rename（原子、零拷贝、零重编码）；跨盘才降级为 copy2+校验+删源。
        关键安全约束：除「跨设备」错误外，任何其它错误（权限/占用/竞态产生的
        文件已存在）一律向上抛，绝不落到 copy2 覆盖分支，守住「绝不覆盖」铁律。
        """
        if os.path.exists(dst) and not Organizer._same_file(src, dst):
            raise FileExistsError(f"目标已存在，拒绝覆盖：{dst}")
        if Organizer._same_file(src, dst):
            # 只是同一个文件改个大小写：Windows 上 rename 直接改就行，
            # 其它平台先借一个临时名中转，避免被当成「覆盖自己」。
            if os.name == "nt":
                os.rename(src, dst)
                return
            tmp = dst + ".__casefix__"
            os.rename(src, tmp)
            os.rename(tmp, dst)
            return
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
                # 清掉不完整的副本；删不掉就改名成 .partial。
                # 否则它会一直卡在那儿被当成「目标已存在」，让这个文件永远搬不过去。
                leftover = ""
                try:
                    os.remove(dst)
                except OSError:
                    try:
                        leftover = dst + ".partial"
                        os.replace(dst, leftover)
                    except OSError:
                        leftover = dst
                raise IOError(
                    "复制后大小不一致，已回滚"
                    + (f"；残留文件已改名为 {os.path.basename(leftover)}，请手动删除"
                       if leftover else "")
                )
            os.remove(src)

    @staticmethod
    def _write_undo(root: str, log: list[dict], kind: str = "organize") -> str:
        """写撤销记录。

        kind 用来区分这条记录是「归类整理」还是「原位重命名」产生的。
        两种模式的记录可能混在同一个根目录里，撤销时必须按模式分开取，
        否则在原地模式点一下撤销，会把以前归类整理那次的移动一起撤掉。
        """
        d = os.path.join(root, UNDO_DIR)
        os.makedirs(d, exist_ok=True)
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(d, 0x02)   # 隐藏这个记录文件夹
        except Exception:
            pass
        # 同一秒内连续归档也要各存一份，绝不互相覆盖，否则撤销会丢记录
        stamp = f"{datetime.now():%Y%m%d_%H%M%S_%f}"   # 毫秒级：同一秒内连续归档也各有各的记录
        p = os.path.join(d, f"undo_{stamp}.json")
        n = 1
        while os.path.exists(p):
            p = os.path.join(d, f"undo_{stamp}_{n:02d}.json")
            n += 1
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"time": time.time(), "root": root, "kind": kind, "moves": log},
                      f, ensure_ascii=False, indent=1)
        # 只保留最近 5 次撤销记录，更旧的自动清理，避免无限堆积
        try:
            all_u = list_undo_files(root)
            for old in all_u[5:]:
                try:
                    os.remove(old)
                except OSError:
                    pass
        except Exception:
            pass
        return p


# --------------------------------------------------------------------------- #
# 撤销
# --------------------------------------------------------------------------- #
def undo_kind_of(path: str) -> str:
    """读出这条撤销记录属于哪种模式。旧版本写的记录没有 kind 字段，
    一律按 'organize' 处理——因为原位重命名模式是后来才加的，历史记录必然来自归类整理。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return str(json.load(f).get("kind") or "organize")
    except Exception:
        return "organize"


def list_undo_files(root: str, kind: str | None = None) -> list[str]:
    """列出某根目录的撤销记录（新→旧）。

    kind=None  → 全部
    kind='organize' / 'inplace' → 只要该模式产生的，避免两种模式互相串台
    """
    d = os.path.join(root, UNDO_DIR)
    if not os.path.isdir(d):
        return []
    fs = [os.path.join(d, f) for f in os.listdir(d) if f.startswith("undo_") and f.endswith(".json")]
    if kind:
        fs = [p for p in fs if undo_kind_of(p) == kind]
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
            _cleanup_empty_dirs(os.path.dirname(dst))   # 搬空的程序自建目录顺手清掉，尽量还原现场
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


def undo_upto(root: str, target_file: str, progress=None,
              kind: str | None = None) -> tuple[int, int, list[str]]:
    """撤销到 target_file 这次归档（含其后所有更新的归档），一次性回到指定状态。

    list_undo_files 已按时间倒序（files[0] 最新）。把 files[0..idx] 全部依次撤销，
    即 E→A 的逆序还原。仍严格不覆盖。
    """
    files = list_undo_files(root, kind=kind)
    if target_file not in files:
        return 0, 0, [f"找不到撤销记录：{os.path.basename(target_file)}"]
    idx = files.index(target_file)
    ok = fail = 0
    errs: list[str] = []
    total = idx + 1
    for i, f in enumerate(files[:total], 1):
        o, f2, e = undo(f)
        ok += o
        fail += f2
        errs.extend(e)
        if progress:
            progress(i, total)
    return ok, fail, errs
