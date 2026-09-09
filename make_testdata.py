# -*- coding: utf-8 -*-
"""
make_testdata.py —— 生成一套「可检测」的照片/视频测试样本，用于外部核查原位重命名功能。

生成的样本覆盖 6 类场景：
  01_EXIF拍摄时间      EXIF 内含 DateTimeOriginal → 应按拍摄时间命名
  02_文件名带日期      无 EXIF，但文件名含日期 → 应从文件名解析
  03_无EXIF无日期      既无 EXIF 也无文件名日期 → 应回退到文件时间
  04_视频              视频格式（无 ffprobe 时走文件名/文件时间）
  05_不该被碰的非照片   非照片格式 → 必须完全不动（文件名 + 内容）
  06_已重命名_跳过      已符合 年[.月.日].分类.序号 格式 → 再跑一次应跳过（幂等）

用法：
    python make_testdata.py [输出目录]        # 默认 ./testdata
"""
import os
import sys
import zipfile

from PIL import Image

# 安全开关：默认 FORCE=False，绝不覆盖任何已存在的文件；
# 仅当显式传入 --force 且用户确认是「专用测试目录」时才放开覆盖限制。
FORCE = False


def _refuse_if_exists(path: str) -> None:
    """默认（FORCE=False）下，凡是已存在的文件一律拒绝写入，避免误覆盖真实照片/文档。"""
    if os.path.exists(path) and not FORCE:
        raise FileExistsError(
            f"拒绝覆盖已存在的文件：{path}\n"
            f"（默认绝不覆盖用户数据。如确认是专用测试目录，可加 --force 覆盖同名文件）"
        )


EXIF_ORIGINAL = 36867      # DateTimeOriginal
EXIF_DIGITIZED = 36868     # DateTimeDigitized
EXIF_DATETIME = 306        # DateTime


def _img(path, exif_dt=None, size=(64, 48), color=(120, 150, 180), fmt=None):
    """生成一张图片；exif_dt 形如 '2021:03:15 10:20:30'，给了就写入 EXIF。"""
    img = Image.new("RGB", size, color)
    kw = {}
    if exif_dt and fmt in (None, "JPEG"):
        ex = img.getexif()
        ex[EXIF_ORIGINAL] = exif_dt
        ex[EXIF_DIGITIZED] = exif_dt
        ex[EXIF_DATETIME] = exif_dt
        kw["exif"] = ex.tobytes()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _refuse_if_exists(path)
    img.save(path, format=fmt, **kw)
    return path


def _fake_video(path, kind):
    """生成带正确文件头的空壳视频（无 ffprobe 时程序会走文件名/文件时间兜底）。"""
    if kind == "mp4":
        head = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 32
    elif kind == "mov":
        head = b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  " + b"\x00" * 32
    elif kind == "flv":
        head = b"FLV\x01\x05\x00\x00\x00\x09" + b"\x00" * 32
    else:
        head = b"\x00" * 32
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _refuse_if_exists(path)
    with open(path, "wb") as f:
        f.write(head)
    return path


def _text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _refuse_if_exists(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def build(root):
    root = os.path.abspath(root)
    if os.path.isdir(root) and os.listdir(root) and not FORCE:
        raise RuntimeError(
            f"目录已存在且非空：{root}\n"
            f"为防止误覆盖真实照片，默认拒绝写入。请换一个空目录 / 不存在的目录；"
            f"如确认是专用测试目录，可加 --force 覆盖同名文件。"
        )
    if os.path.isdir(root):
        print(f"[i] 已存在目录 {root}，将在其中生成/覆盖测试样本（默认绝不碰目录里其它文件）")
    made = []

    # ---- 01 EXIF 里有拍摄时间 ----
    d = os.path.join(root, "01_EXIF拍摄时间")
    made += [
        _img(os.path.join(d, "IMG_20210315.jpg"), "2021:03:15 10:20:30", color=(90, 130, 170)),
        _img(os.path.join(d, "IMG_20220720.jpg"), "2022:07:20 14:05:00", color=(170, 130, 90)),
        _img(os.path.join(d, "IMG_20231105.jpg"), "2023:11:05 09:30:15", color=(120, 170, 120)),
    ]

    # ---- 02 无 EXIF，但文件名含日期 ----
    d = os.path.join(root, "02_文件名带日期")
    made += [
        _img(os.path.join(d, "Screenshot_20210701.png"), None, fmt="PNG", color=(200, 200, 210)),
        _img(os.path.join(d, "屏幕截图 2021-08-01 101010.png"), None, fmt="PNG", color=(210, 205, 195)),
        _fake_video(os.path.join(d, "VID_20220815.mp4"), "mp4"),
    ]

    # ---- 03 既无 EXIF 也无文件名日期 → 回退文件时间 ----
    d = os.path.join(root, "03_无EXIF无日期")
    made += [
        _img(os.path.join(d, "old_01.jpg"), None, color=(150, 120, 150)),
        _img(os.path.join(d, "old_02.jpg"), None, color=(150, 150, 120)),
        _img(os.path.join(d, "pic_webp.webp"), None, fmt="WEBP", color=(130, 160, 160)),
        _img(os.path.join(d, "anim.gif"), None, fmt="GIF", color=(160, 140, 160)),
    ]

    # ---- 04 视频 ----
    d = os.path.join(root, "04_视频")
    made += [
        _fake_video(os.path.join(d, "clip_20210909.mov"), "mov"),
        _fake_video(os.path.join(d, "movie.flv"), "flv"),
    ]

    # ---- 05 非照片：必须完全不动（对照组） ----
    d = os.path.join(root, "05_不该被碰的非照片")
    made += [
        _text(os.path.join(d, "重要文档.docx.txt"), "这是文档内容，程序不该碰我。"),
        _text(os.path.join(d, "说明.txt"), "纯文本，程序不该碰我。"),
        _text(os.path.join(d, "数据.csv"), "a,b,c\n1,2,3\n"),
    ]
    # 造一个真正的 .docx / .zip（二进制），验证非照片二进制也不被动
    os.makedirs(d, exist_ok=True)
    docx = os.path.join(d, "重要文档.docx")
    _refuse_if_exists(docx)
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
    made.append(docx)

    # ---- 06 已符合命名格式 → 再跑应跳过（幂等组） ----
    d = os.path.join(root, "06_已重命名_跳过")
    made += [
        _img(os.path.join(d, "2023.风景.01.jpg"), "2023:05:01 08:00:00", color=(100, 160, 200)),
        _img(os.path.join(d, "2021.截图.02.png"), None, fmt="PNG", color=(220, 215, 205)),
    ]

    print(f"[✓] 测试样本已生成：{root}")
    print(f"    共 {len(made)} 个文件")
    for p in made:
        print(f"      {os.path.relpath(p, root)}")
    return made


if __name__ == "__main__":
    argv = sys.argv[1:]
    FORCE = "--force" in argv
    pos = [a for a in argv if not a.startswith("--")]
    out = pos[0] if pos else os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata")
    out = os.path.abspath(out)
    if os.path.isdir(out) and os.listdir(out) and not FORCE:
        print(f"[×] 目录已存在且非空：{out}")
        print("    为防止误覆盖真实照片，默认拒绝写入。")
        print("    请换一个空目录 / 不存在的目录；如确认是专用测试目录，加 --force。")
        sys.exit(1)
    try:
        build(out)
    except (FileExistsError, RuntimeError) as e:
        print(f"[×] {e}")
        sys.exit(1)
