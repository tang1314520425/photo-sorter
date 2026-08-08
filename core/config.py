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

    @classmethod
    def load(cls, path: str = SETTINGS_FILE) -> "Settings":
        if not os.path.isfile(path):
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            base = cls()
            for k, v in data.items():
                if hasattr(base, k):
                    setattr(base, k, v)
            return base
        except Exception:
            return cls()

    def save(self, path: str = SETTINGS_FILE) -> None:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            pass
