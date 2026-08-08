# -*- coding: utf-8 -*-
"""媒体读取层：把图片 / RAW / 视频统一转成一张内存中的小缩略图，供识别使用。

铁律：本模块只读。任何情况下都不会写入、修改、重编码原文件。
所有缩放只发生在内存副本上，原文件的字节一个都不会变。
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime

from PIL import Image, ImageFile

from .config import IMAGE_EXTS, RAW_EXTS, VIDEO_EXTS

ImageFile.LOAD_TRUNCATED_IMAGES = True   # 半损坏的图也尽量读出来，只影响内存副本
Image.MAX_IMAGE_PIXELS = 200_000_000     # 上限 ~2 亿像素（约 14K×14K），放开常见大图，仍挡住恶意炸弹图

THUMB_SIZE = 384                         # 送进模型前的内存缩略图边长

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class MediaInfo:
    path: str
    ext: str
    kind: str                 # image / raw / video
    width: int = 0
    height: int = 0
    is_animated: bool = False
    frames: int = 1
    shot_time: float = 0.0    # 拍摄时间（EXIF 优先，否则文件时间），用于排序
    duration: float = 0.0     # 视频时长（秒）
    thumb: Image.Image | None = None
    extra_frames: list = field(default_factory=list)
    error: str = ""


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def kind_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in RAW_EXTS:
        return "raw"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return ""


def _file_time(path: str) -> float:
    try:
        st = os.stat(path)
        return min(st.st_mtime, getattr(st, "st_ctime", st.st_mtime))
    except OSError:
        return 0.0


def _exif_time(img: Image.Image) -> float:
    try:
        exif = img.getexif()
        if not exif:
            return 0.0
        for tag in (36867, 36868, 306):   # DateTimeOriginal / DateTimeDigitized / DateTime
            raw = exif.get(tag)
            if raw:
                s = str(raw).strip().replace("/", ":")
                try:
                    return datetime.strptime(s[:19], "%Y:%m:%d %H:%M:%S").timestamp()
                except ValueError:
                    continue
    except Exception:
        pass
    return 0.0


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGB",):
        return img
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        conv = img.convert("RGBA")
        bg.paste(conv, mask=conv.split()[-1])
        return bg
    return img.convert("RGB")


# --------------------------------------------------------------------------- #
# 普通图片
# --------------------------------------------------------------------------- #
def _read_image(path: str, info: MediaInfo) -> None:
    with Image.open(path) as img:          # 只读打开，with 结束即关闭，不 save
        info.width, info.height = img.size
        info.frames = int(getattr(img, "n_frames", 1) or 1)
        info.is_animated = bool(getattr(img, "is_animated", False)) or info.frames > 1
        info.shot_time = _exif_time(img)

        # JPEG 走 draft 快速降采样解码（只影响内存副本，速度快好几倍）
        try:
            img.draft("RGB", (THUMB_SIZE, THUMB_SIZE))
        except Exception:
            pass

        if info.is_animated:
            try:
                img.seek(min(1, info.frames - 1))   # 动图取第 2 帧，通常比首帧有内容
            except Exception:
                pass

        frame = img.copy()

    frame = _to_rgb(frame)
    frame.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.BILINEAR)
    info.thumb = frame


# --------------------------------------------------------------------------- #
# RAW
# --------------------------------------------------------------------------- #
def _read_raw(path: str, info: MediaInfo) -> None:
    try:
        import rawpy
    except ImportError:
        info.error = "未安装 rawpy，RAW 只按规则归类"
        return

    with rawpy.imread(path) as raw:        # 只读
        # 优先取相机内嵌的 JPEG 预览图，毫秒级，不解 RAW
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                img = Image.open(io.BytesIO(thumb.data))
                img = _to_rgb(img)
            else:
                img = Image.fromarray(thumb.data)
        except Exception:
            rgb = raw.postprocess(half_size=True, use_camera_wb=True, no_auto_bright=False)
            img = Image.fromarray(rgb)
        try:
            info.width, info.height = raw.sizes.width, raw.sizes.height
        except Exception:
            info.width, info.height = img.size

    img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.BILINEAR)
    info.thumb = img


# --------------------------------------------------------------------------- #
# 视频（用 ffmpeg 抽一帧到内存，绝不写盘、绝不转码原视频）
# --------------------------------------------------------------------------- #
_FFMPEG: str | None = None
_FFMPEG_CHECKED = False

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)")
_SIZE_RE = re.compile(r"(\d{2,5})x(\d{2,5})")


def ffmpeg_path() -> str | None:
    global _FFMPEG, _FFMPEG_CHECKED
    if _FFMPEG_CHECKED:
        return _FFMPEG
    _FFMPEG_CHECKED = True
    try:
        import imageio_ffmpeg
        _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        from shutil import which
        _FFMPEG = which("ffmpeg")
    return _FFMPEG


def _run(cmd: list[str], timeout: int = 40) -> tuple[bytes, str, int]:
    p = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, creationflags=_CREATE_NO_WINDOW,
    )
    return p.stdout, p.stderr.decode("utf-8", "ignore"), p.returncode


def _probe_video(exe: str, path: str, info: MediaInfo) -> None:
    _, err, _ = _run([exe, "-hide_banner", "-i", path], timeout=30)
    m = _DUR_RE.search(err)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        info.duration = h * 3600 + mi * 60 + s
    for line in err.splitlines():
        if "Video:" in line:
            sm = _SIZE_RE.search(line)
            if sm:
                info.width, info.height = int(sm.group(1)), int(sm.group(2))
            break


def _grab_frame(exe: str, path: str, at: float) -> Image.Image | None:
    cmd = [exe, "-hide_banner", "-loglevel", "error"]
    if at > 0.05:
        cmd += ["-ss", f"{at:.2f}"]
    cmd += [
        "-i", path, "-frames:v", "1",
        "-vf", f"scale={THUMB_SIZE}:-2:flags=fast_bilinear",
        "-f", "image2", "-c:v", "mjpeg", "-q:v", "6", "-",
    ]
    try:
        out, _, code = _run(cmd, timeout=45)
    except subprocess.TimeoutExpired:
        return None
    if code != 0 or len(out) < 512:
        return None
    try:
        return _to_rgb(Image.open(io.BytesIO(out)))
    except Exception:
        return None


def _read_video(path: str, info: MediaInfo, extra_frames: bool = True) -> list[Image.Image]:
    exe = ffmpeg_path()
    if not exe:
        info.error = "找不到 ffmpeg，视频无法识别内容"
        return []
    _probe_video(exe, path, info)
    info.shot_time = _file_time(path)

    if info.duration > 1:
        points = [info.duration * r for r in ((0.15, 0.45, 0.75) if extra_frames else (0.3,))]
    else:
        points = [0.0]

    frames: list[Image.Image] = []
    for t in points:
        f = _grab_frame(exe, path, t)
        if f is not None:
            f.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.BILINEAR)
            frames.append(f)
    if frames:
        info.thumb = frames[0]
    else:
        info.error = info.error or "视频抽帧失败"
    return frames


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #
def probe(path: str, video_frames: bool = True) -> MediaInfo:
    """读取一个媒体文件的基本信息 + 内存缩略图。永远不修改原文件。"""
    ext = os.path.splitext(path)[1].lower()
    info = MediaInfo(path=path, ext=ext, kind=kind_of(path))
    info.shot_time = _file_time(path)
    info.extra_frames = []          # type: ignore[attr-defined]

    try:
        if info.kind == "image":
            _read_image(path, info)
        elif info.kind == "raw":
            _read_raw(path, info)
        elif info.kind == "video":
            frames = _read_video(path, info, extra_frames=video_frames)
            info.extra_frames = frames[1:]
        else:
            info.error = "不支持的格式"
    except Exception as e:                      # 单个文件读失败不能影响整批
        info.error = f"{type(e).__name__}: {e}"

    if not info.shot_time:
        info.shot_time = _file_time(path)
    return info
