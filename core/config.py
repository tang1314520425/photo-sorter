# -*- coding: utf-8 -*-
"""类别清单与运行配置的读写。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATEGORIES_FILE = os.path.join(APP_DIR, "categories.json")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")

# 支持的扩展名（全部小写，含点）
IMAGE_EXTS = {".jpg", ".jpeg", ".jpe", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
RAW_EXTS = {
    ".raw", ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng",
    ".orf", ".rw2", ".raf", ".pef", ".srw", ".x3f", ".3fr", ".erf", ".kdc", ".mrw",
}
VIDEO_EXTS = {".mp4", ".mov", ".flv", ".m4v", ".avi", ".mkv", ".wmv", ".webm", ".3gp", ".mpg", ".mpeg"}
ALL_EXTS = IMAGE_EXTS | RAW_EXTS | VIDEO_EXTS


@dataclass
class Category:
    name: str
    enabled: bool = True
    rule: str | None = None          # animated / screenshot / video / None
    prompts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "enabled": self.enabled, "rule": self.rule, "prompts": list(self.prompts)}


@dataclass
class CategoryBook:
    fallback: str = "其它"
    min_confidence: float = 0.22
    categories: list[Category] = field(default_factory=list)

    # ---------- 读写 ----------
    @classmethod
    def load(cls, path: str = CATEGORIES_FILE) -> "CategoryBook":
        if not os.path.isfile(path):
            return cls(categories=[])
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cats = []
        for item in data.get("categories", []):
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            cats.append(
                Category(
                    name=name,
                    enabled=bool(item.get("enabled", True)),
                    rule=item.get("rule") or None,
                    prompts=[str(p) for p in (item.get("prompts") or [])],
                )
            )
        return cls(
            fallback=str(data.get("fallback", "其它")) or "其它",
            min_confidence=float(data.get("min_confidence", 0.22)),
            categories=cats,
        )

    def save(self, path: str = CATEGORIES_FILE) -> None:
        payload = {
            "version": 1,
            "_说明": "类别清单可直接编辑本文件，或在程序界面中增删改。name=文件夹名与编号前缀；"
                     "prompts=英文语义描述（喂给本地视觉模型，写得越具体越准）；"
                     "rule=特殊规则(animated/screenshot/video)，有 rule 的类别优先于语义判断；enabled=是否启用。",
            "fallback": self.fallback,
            "min_confidence": self.min_confidence,
            "categories": [c.to_dict() for c in self.categories],
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # ---------- 查询 ----------
    @property
    def enabled_categories(self) -> list[Category]:
        return [c for c in self.categories if c.enabled]

    def rule_category(self, rule: str) -> Category | None:
        for c in self.enabled_categories:
            if c.rule == rule:
                return c
        return None

    def semantic_categories(self) -> list[Category]:
        """参与语义打分的类别（有 prompts 的都参与，包括带 rule 的截图类）。"""
        return [c for c in self.enabled_categories if c.prompts]

    def all_folder_names(self) -> set[str]:
        names = {c.name for c in self.categories}
        names.add(self.fallback)
        return names


def _coerce(field_name: str, want: type, got):
    """把配置值纠正成该字段应有的类型；纠不回来就退回默认值。

    宁可让一项设置回到默认，也不能让程序带着一个错类型的值跑下去。
    """
    default = Settings().__getattribute__(field_name)
    try:
        if want is bool:
            if isinstance(got, bool):
                return got
            if isinstance(got, str):
                return got.strip().lower() in ("1", "true", "yes", "on", "是")
            return bool(got)
        if want is float:
            return float(got)
        if want is int:
            return int(got)
        if want is str:
            return default if got is None else str(got)
        if want is list:
            if isinstance(got, list):
                return got
            if isinstance(got, str):        # 单路径当成长度 1 的列表，别丢用户的设置
                return [got] if got.strip() else []
            return list(default)
    except (TypeError, ValueError):
        return default
    return got


@dataclass
class Settings:
    """界面上的运行参数，会记住上次选择。"""
    date_tag: str = ""                # 手动日期段：期日编号未勾选时生效；留空 = 纯 1、2、3 序号
    recursive: bool = False           # 是否连子文件夹里的文件一起整理
    video_as_own_category: bool = False   # 视频是否单独归到「视频」文件夹
    dry_run: bool = True              # 预演（只看结果不动文件）
    min_confidence: float = 0.22
    keep_original_name: bool = False  # 重命名时在末尾保留原文件名
    last_folder: str = ""

    # —— 新增：命名模式（2026-08-08）——
    date_numbering: bool = False      # 期日编号：用照片创建时间自动带日期段，否则用上面的手动 date_tag
    date_granularity: str = "day"     # 期日编号粒度：year / month / day（年 / 年-月 / 年-月-日）
    time_block: bool = False          # 时间板块：按时间把同类文件分到子目录（年 / 年-月）
    time_block_granularity: str = "year"  # 时间板块粒度：year / month
    date_source: str = "exif"         # 命名日期来源：exif=拍摄时间(EXIF优先) / ctime=文件创建时间(Windows创建时间)

    # —— 语义识别开关（2026-09-02）——
    use_ai: bool = True               # 关掉则只按规则整理（动图/截图/视频），不加载视觉模型，省内存省时间

    # —— 模式（2026-09-03）——
    mode: str = "organize"            # organize=归类整理（移入分类文件夹）/ inplace=原位重命名（只改名不移动）
    inplace_date_granularity: str = "year"   # 原位重命名日期粒度：year（2023.风景.01）/ ymd（2023.08.15.风景.01）
    scan_roots: list[str] = field(default_factory=list)   # 原位重命名模式勾选的扫描根目录

    @classmethod
    def load(cls, path: str = SETTINGS_FILE) -> "Settings":
        """读设置。文件损坏要明说，不能悄悄回默认——否则用户只会觉得「程序忘了我的设置」。"""
        if not os.path.isfile(path):
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"设置文件已损坏（{os.path.basename(path)} 第 {e.lineno} 行：{e.msg}）"
            ) from e
        except UnicodeDecodeError as e:
            raise ValueError(f"设置文件编码异常（{os.path.basename(path)}）：{e}") from e
        except OSError as e:
            raise ValueError(f"设置文件读不出来（{os.path.basename(path)}）：{e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"设置文件格式不对（{os.path.basename(path)}），本该是键值对的")
        base = cls()
        # 逐字段校验类型。settings.json 是明文，用户手改过、或者从旧版本
        # 升级上来时字段类型可能不对（比如 min_confidence 变成字符串、
        # scan_roots 变成单个路径字符串）。以前是直接塞进去，
        # 结果要等到真正用到那一刻才崩，而且崩在莫名其妙的地方。
        for k, v in data.items():
            if not hasattr(base, k):
                continue
            setattr(base, k, _coerce(k, type(getattr(base, k)), v))
        return base

    def save(self, path: str = SETTINGS_FILE) -> None:
        """写设置。失败一律往上抛，交给界面提示，绝不静默吞掉。

        以前这里是 except Exception: pass，磁盘满 / 目录只读 / 杀软拦截时用户毫无察觉，
        下次启动发现设置全回默认，还以为是程序有毛病。
        """
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:                       # 写坏一半的临时文件要清掉，别留垃圾
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise
