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
from datetime import datetime, timezone, timedelta

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
    time_source: str = ""     # 时间来源：exif / filename / filetime / ctime / video_meta / unknown
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


def _create_time(path: str) -> float:
    """文件创建时间（Windows 上为 st_ctime；Linux/macOS st_ctime 是元数据变更时间，非真实创建时间）。

    仅当 date_source='ctime' 模式使用，作为命名日期来源。
    """
    try:
        st = os.stat(path)
        ct = getattr(st, "st_ctime", 0.0)
        return ct or _file_time(path)
    except OSError:
        return 0.0


def _exif_time_pil(img: Image.Image) -> tuple[float, str]:
    """用 Pillow 读主 IFD 的拍摄时间，返回 (时间戳, 来源)。读不到返回 (0, '')。"""
    try:
        exif = img.getexif()
        if not exif:
            return 0.0, ""
        for tag in (36867, 36868, 306):   # DateTimeOriginal / DateTimeDigitized / DateTime
            raw = exif.get(tag)
            if raw:
                s = str(raw).strip().replace("/", ":")
                try:
                    ts = safe_timestamp(datetime.strptime(s[:19], "%Y:%m:%d %H:%M:%S"))
                except ValueError:
                    continue
                if ts:                     # 0000:00:00 这类空日期要跳过，别当成有效时间
                    return ts, "exif"
    except Exception:
        pass
    return 0.0, ""


def _exif_time_exifread(path: str) -> tuple[float, str]:
    """Pillow 读不到时，用 exifread 读更完整的 EXIF（SubIFD / MakerNote）。只读不写。"""
    try:
        import exifread
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False, stop_tag="DateTimeOriginal")
        for tag in ("EXIF DateTimeOriginal", "EXIF DateTimeDigitized",
                    "Image DateTimeOriginal", "Image DateTimeDigitized", "Image DateTime"):
            raw = tags.get(tag)
            if raw:
                s = str(raw).strip().replace("/", ":")
                try:
                    ts = safe_timestamp(datetime.strptime(s[:19], "%Y:%m:%d %H:%M:%S"))
                except ValueError:
                    continue
                if ts:
                    return ts, "exif"
    except Exception:
        pass
    return 0.0, ""


_NAME_PATTERNS = [
    re.compile(r"(\d{4})[-_.]?(\d{2})[-_.]?(\d{2})[-_.]?(\d{2})(\d{2})(\d{2})"),  # IMG_20260825_123456 / 2026.08.25.123456
    re.compile(r"(\d{4})[-_.]?(\d{2})[-_.]?(\d{2})"),                            # 2026-08-25 / 20260825 / 2026.08.25
]


def _name_time(path: str) -> tuple[float, str]:
    """从文件名解析拍摄日期（手机原文件名常自带日期），作为 EXIF 缺失时的兜底。

    注意：datetime(...).timestamp() 在 Windows 上遇到 1970 年之前（或远超 9999 年）的
    日期会抛 OSError，而不是 ValueError。只捕获 ValueError 会让异常一路传播出 probe()，
    导致「整个批次扫描直接崩掉」。所以这里必须三个都接住。
    """
    base = os.path.splitext(os.path.basename(path))[0]
    for pat in _NAME_PATTERNS:
        m = pat.search(base)
        if not m:
            continue
        g = m.groups()
        try:
            if len(g) >= 6:
                y, mo, d, h, mi, s = (int(x) for x in g[:6])
                dt = datetime(y, mo, d, h, mi, s)
            else:
                y, mo, d = (int(x) for x in g[:3])
                dt = datetime(y, mo, d)
            return safe_timestamp(dt), "filename"
        except (ValueError, OSError, OverflowError):
            continue
    return 0.0, ""


def safe_timestamp(dt: datetime) -> float:
    """datetime → 时间戳，越界（Windows 上 1970 年前 / 9999 年后会抛错）时返回 0。

    文件名里什么数字都可能出现（IMG_00011231、扫描件 99991232），
    不能让一个畸形日期把整批扫描搞崩。
    """
    try:
        return dt.timestamp()
    except (ValueError, OSError, OverflowError):
        return 0.0


def safe_datetime(ts: float) -> datetime | None:
    """时间戳 → datetime，越界时返回 None（调用方按「取不到时间」处理）。"""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts)
    except (ValueError, OSError, OverflowError):
        return None


_VIDEO_CREATION_RE = re.compile(
    r"creation_time\s*:\s*(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?"
)


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
        info.shot_time, info.time_source = _exif_time_pil(img)
        if not info.shot_time:
            info.shot_time, info.time_source = _exif_time_exifread(path)

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
    # RAW 里同样藏着完整的 EXIF（拍摄时间）。以前这里根本没读，
    # 导致 .CR2/.NEF/.ARW/.DNG 全部退化成「文件修改时间」——
    # 相机里设好了日期却用不上，整理完的照片年份全是错的。
    try:
        ts, src = _exif_time_exifread(path)
        if ts:
            info.shot_time, info.time_source = ts, src
    except Exception:
        pass

    try:
        import rawpy
    except ImportError:
        info.error = "未安装 rawpy，RAW 只按规则归类"
        return

    img: Image.Image | None = None          # 兜底定义：下面两条路都失败时不会抛 UnboundLocalError
    try:
        with rawpy.imread(path) as raw:     # 只读
            # 优先取相机内嵌的 JPEG 预览图，毫秒级，不解 RAW
            try:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    img = Image.open(io.BytesIO(thumb.data))
                    img = _to_rgb(img)
                else:
                    img = Image.fromarray(thumb.data)
            except Exception:
                try:
                    rgb = raw.postprocess(half_size=True, use_camera_wb=True,
                                          no_auto_bright=False)
                    img = Image.fromarray(rgb)
                except Exception as e:
                    info.error = f"RAW 解码失败：{type(e).__name__}"
            try:
                info.width, info.height = raw.sizes.width, raw.sizes.height
            except Exception:
                if img is not None:
                    info.width, info.height = img.size
    except Exception as e:
        info.error = info.error or f"RAW 读取失败：{type(e).__name__}: {e}"

    if img is None:
        # 缩略图拿不到（很常见：rawpy 不支持的机型/损坏文件），
        # 但 EXIF 时间已经取到了，照样能正确归类命名，只是没有画面可识别。
        info.error = info.error or "RAW 无法解码出预览图，按规则归类"
        return

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
    cm = _VIDEO_CREATION_RE.search(err)
    if cm:
        try:
            dt = datetime(*(int(x) for x in cm.groups()[:6]))
            tz = cm.group(7)
            if tz == "Z":
                # ffmpeg 输出 UTC（带 Z），转换成当地时区显示
                dt = dt.replace(tzinfo=timezone.utc).astimezone()
            elif tz and tz[0] in "+-":
                # 已带时区偏移（如 +08:00），按偏移转本地
                sign = 1 if tz[0] == "+" else -1
                hh = int(tz[1:3]); mm = int(tz[4:6]) if len(tz) >= 6 else 0
                dt = dt.replace(tzinfo=timezone(sign * timedelta(hours=hh, minutes=mm))).astimezone()
            ts = safe_timestamp(dt)      # 越界日期（0000-00-00 等）不能让整批扫描崩掉
            if ts:
                info.shot_time = ts
                info.time_source = "video_meta"
        except (ValueError, OSError, OverflowError):
            pass
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
    # info.shot_time 优先用视频内部 creation_time（见 _probe_video）；取不到由 probe() 统一兜底

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
def probe(path: str, video_frames: bool = True, date_source: str = "exif") -> MediaInfo:
    """读取一个媒体文件的基本信息 + 内存缩略图。永远不修改原文件。

    date_source:
      'exif' -> 命名日期优先用照片拍摄时间（EXIF DateTimeOriginal 等），取不到才用文件时间
      'ctime'-> 统一用文件创建时间（Windows 上为 st_ctime）
    """
    ext = os.path.splitext(path)[1].lower()
    info = MediaInfo(path=path, ext=ext, kind=kind_of(path))
    info.shot_time = _file_time(path)
    info.time_source = "filetime"   # 默认兜底；后续读到 EXIF/文件名/视频元数据会覆盖
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

    if date_source == "ctime":
        # 强制使用文件创建时间（Windows 上为 st_ctime），忽略 EXIF / 文件名
        info.shot_time = _create_time(path)
        info.time_source = "ctime"
        return info

    # date_source == 'exif'（默认）：EXIF/视频内部元数据 优先 → 文件名自带日期 → 文件时间
    #
    # 注意：进到这里时 shot_time 已被第 346 行预置成文件时间（必然非空），
    # 所以绝不能用 `if not info.shot_time` 来判断「还没拿到拍摄时间」，
    # 否则文件名兜底永远轮不到 —— 视频尤其明显：VID_20220815.mp4 这类
    # 相机原名自带的日期会被跳过，退化成文件系统的修改时间。
    # 这里改为按 time_source 判断：只有尚未拿到 exif / video_meta 时才走文件名。
    # 整个兜底链都必须在 try 里：这一段在 try(文件读取) 之外，
    # 只要有一行抛异常，就会一路崩到调用方，让「整批扫描」挂掉。
    # 宁可退化成文件时间，也绝不能因为一个畸形文件名中断整批任务。
    try:
        if info.time_source not in ("exif", "video_meta"):
            ts, src = _name_time(path)  # 手机/相机原名常自带拍摄日期，优先于文件系统时间
            if ts:
                info.shot_time, info.time_source = ts, src
        if info.time_source in ("", "filetime"):
            if not info.shot_time:
                info.shot_time = _file_time(path)
            info.time_source = info.time_source or "filetime"
        if not info.shot_time:
            info.time_source = "unknown"
    except Exception as e:              # 兜底失败也要能返回，只是时间来源退化为文件时间
        info.error = info.error or f"时间解析异常：{type(e).__name__}: {e}"
        if not info.shot_time:
            try:
                info.shot_time = _file_time(path)
                info.time_source = "filetime"
            except Exception:
                info.time_source = "unknown"
    return info
