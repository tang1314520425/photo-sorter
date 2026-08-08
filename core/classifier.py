# -*- coding: utf-8 -*-
"""分类引擎。

两层结构：
  第一层 规则引擎：动图、截图、视频这类有硬特征的，直接判定，零成本零误差。
  第二层 语义引擎：本地 CLIP 视觉模型，把图片和「类别的英文描述」放到同一个向量空间比相似度。
                   完全离线、跑在 CPU 上、不联网、不花钱、不消耗任何积分。
若第二层没装，程序照常工作，只是语义类别会落到「其它」，并明确告知。
"""

from __future__ import annotations

import os
import re
import threading

# 必须在 import 任何 huggingface 相关库之前设置，否则国内下载模型会卡死
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from .config import CategoryBook, Category
from .media import MediaInfo

MODEL_NAME = "ViT-B-32"
MODEL_PRETRAINED = "laion2b_s34b_b79k"

# 完全离线分发：模型权重随 exe 内置（models/open_clip_model.safetensors）。
# 打包后 PyInstaller 会把文件放在 sys._MEIPASS/models/ 下；运行时优先从这里读，
# 不联网、不下载。开发态（未打包）返回 None，走原「首次联网下载」逻辑。
def _bundle_weights_path():
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = os.path.join(base, "models", "open_clip_model.safetensors")
        if os.path.exists(p):
            return p
    return None


# 常见屏幕/手机分辨率，用于辅助识别截图
_SCREEN_SIZES = {
    (1920, 1080), (1366, 768), (2560, 1440), (3840, 2160), (1440, 900),
    (1600, 900), (1680, 1050), (2880, 1800), (1280, 720), (1512, 982),
    (1080, 1920), (750, 1334), (1125, 2436), (1170, 2532), (1179, 2556),
    (1284, 2778), (1440, 2560), (1440, 3200), (1080, 2340), (1080, 2400),
    (828, 1792), (1242, 2688), (720, 1280), (1200, 2670), (1216, 2688),
    (2560, 1080), (3440, 1440), (3840, 1080), (5120, 1440),
}
# 截图文件名判定：
#  · 明确截图词（screenshot / screen shot / 屏幕截图 / 截屏 / snapshot_ / scr_）出现即可
#  · 裸「截图」二字必须不在中文词中间（如「旅行截图记录」不算），仅匹配开头或带分隔符的写法
_SHOT_NAME_RE = re.compile(
    r"(?:^|[\s_\-])(screenshot|screen[ _-]?shot|屏幕截图|截屏|snapshot_?\d|scr_\d)"
    r"|(?<![\u4e00-\u9fff])截图",
    re.I,
)


class ClassifyResult:
    __slots__ = ("category", "confidence", "reason", "scores")

    def __init__(self, category: str, confidence: float, reason: str, scores=None):
        self.category = category
        self.confidence = confidence
        self.reason = reason
        self.scores = scores or []


# --------------------------------------------------------------------------- #
class SemanticEngine:
    """本地 CLIP 引擎，懒加载。"""

    def __init__(self) -> None:
        self.available = False
        self.status = "未加载"
        self._model = None
        self._preprocess = None
        self._tokenizer = None
        self._torch = None
        self._text_feats = None          # (类别数, 维度)
        self._text_key = ""
        self._lock = threading.Lock()

    # ---------- 依赖检测 ----------
    @staticmethod
    def deps_installed() -> bool:
        try:
            import torch  # noqa: F401
            import open_clip  # noqa: F401
            return True
        except Exception:
            return False

    # ---------- 加载模型 ----------
    def load(self, progress=None) -> tuple[bool, str]:
        with self._lock:
            if self.available:
                return True, self.status
            try:
                import torch
                import open_clip
                from safetensors.torch import load_file

                torch.set_grad_enabled(False)
                try:
                    torch.set_num_threads(max(2, (os.cpu_count() or 4) // 2))
                except Exception:
                    pass

                # 完全离线分发：权重随 exe 内置，优先从 bundle 读取，绝不联网
                bundled = _bundle_weights_path()
                if bundled:
                    if progress:
                        progress("正在载入内置视觉模型（完全离线，无需下载）…")
                    model, _, preprocess = open_clip.create_model_and_transforms(
                        MODEL_NAME, pretrained=False
                    )
                    model.load_state_dict(load_file(bundled))
                else:
                    if progress:
                        progress("正在载入本地视觉模型…（首次需下载约 600MB，之后永久离线可用）")
                    model, _, preprocess = open_clip.create_model_and_transforms(
                        MODEL_NAME, pretrained=MODEL_PRETRAINED
                    )
                model.eval()
                self._torch = torch
                self._model = model
                self._preprocess = preprocess
                self._tokenizer = open_clip.get_tokenizer(MODEL_NAME)
                self.available = True
                self.status = f"本地视觉模型已就绪（{MODEL_NAME}，CPU 运行，0 积分）"
                if progress:
                    progress(self.status)
                return True, self.status
            except Exception as e:
                self.available = False
                self.status = f"视觉模型不可用：{type(e).__name__}: {e}"
                if progress:
                    progress(self.status)
                return False, self.status

    # ---------- 类别文本向量 ----------
    def build_text_features(self, cats: list[Category]) -> None:
        if not self.available:
            return
        key = "|".join(c.name + "#" + ";".join(c.prompts) for c in cats)
        if key == self._text_key and self._text_feats is not None:
            return
        torch = self._torch
        vecs = []
        for c in cats:
            toks = self._tokenizer(c.prompts)
            feat = self._model.encode_text(toks)
            feat = feat / feat.norm(dim=-1, keepdim=True)
            feat = feat.mean(dim=0)                       # 多条描述取平均，更稳
            feat = feat / feat.norm()
            vecs.append(feat)
        self._text_feats = torch.stack(vecs)
        self._text_key = key

    # ---------- 打分 ----------
    def score(self, images) -> list[float]:
        """输入若干张 PIL 图（视频=多帧），返回每个类别的概率。"""
        if not self.available or self._text_feats is None or not images:
            return []
        torch = self._torch
        batch = torch.stack([self._preprocess(im) for im in images])
        feats = self._model.encode_image(batch)
        feats = feats / feats.norm(dim=-1, keepdim=True)
        logits = 100.0 * feats @ self._text_feats.T          # (帧数, 类别数)
        probs = logits.softmax(dim=-1).mean(dim=0)           # 多帧取平均
        return [float(x) for x in probs]


# --------------------------------------------------------------------------- #
class Classifier:
    def __init__(self, book: CategoryBook, use_ai: bool = True) -> None:
        self.book = book
        self.use_ai = use_ai
        self.engine = SemanticEngine()
        self._sem_cats: list[Category] = []

    # ---------- 准备 ----------
    def prepare(self, progress=None) -> tuple[bool, str]:
        self._sem_cats = self.book.semantic_categories()
        if not self.use_ai:
            return False, "已关闭语义识别，仅按规则整理（动图/截图/视频）"
        if not SemanticEngine.deps_installed():
            return False, "未安装视觉模型依赖，仅按规则整理。装好后重开程序即可自动启用语义识别"
        ok, msg = self.engine.load(progress)
        if ok and self._sem_cats:
            if progress:
                progress("正在编码类别描述…")
            self.engine.build_text_features(self._sem_cats)
        return ok, msg

    # ---------- 规则层 ----------
    def _rule_hit(self, info: MediaInfo, video_own: bool) -> Category | None:
        if info.kind == "video" and video_own:
            c = self.book.rule_category("video")
            if c:
                return c
        if info.is_animated:
            c = self.book.rule_category("animated")
            if c:
                return c
        if info.kind == "image":
            c = self.book.rule_category("screenshot")
            if c:
                name = os.path.basename(info.path)
                if _SHOT_NAME_RE.search(name):
                    return c
                if (info.width, info.height) in _SCREEN_SIZES:
                    return c
        return None

    # ---------- 主入口 ----------
    def classify(self, info: MediaInfo, video_own: bool = False,
                 min_conf: float | None = None) -> ClassifyResult:
        fallback = self.book.fallback
        if min_conf is None:
            min_conf = self.book.min_confidence

        hit = self._rule_hit(info, video_own)
        if hit is not None:
            return ClassifyResult(hit.name, 1.0, "规则判定")

        if info.thumb is None:
            return ClassifyResult(fallback, 0.0, info.error or "无法读取画面")

        if not (self.engine.available and self._sem_cats):
            return ClassifyResult(fallback, 0.0, "未启用语义识别")

        images = [info.thumb] + list(getattr(info, "extra_frames", []) or [])
        try:
            probs = self.engine.score(images)
        except Exception as e:
            return ClassifyResult(fallback, 0.0, f"识别出错：{type(e).__name__}")
        if not probs:
            return ClassifyResult(fallback, 0.0, "识别无结果")

        pairs = sorted(zip(self._sem_cats, probs), key=lambda x: x[1], reverse=True)
        top_cat, top_p = pairs[0]
        top3 = [(c.name, round(p, 3)) for c, p in pairs[:3]]

        if top_p < min_conf:
            return ClassifyResult(fallback, top_p, f"把握不足({top_p:.0%})，最像 {top_cat.name}", top3)
        return ClassifyResult(top_cat.name, top_p, f"语义识别 {top_p:.0%}", top3)
