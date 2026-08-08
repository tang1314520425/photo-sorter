# -*- coding: utf-8 -*-
"""自检脚本：造一批样本 → 走完整流程 → 逐字节校验原文件没被动过 → 再撤销还原。

用法：  python selftest.py
它只在系统临时目录里操作，不会碰你的任何真实照片。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw

from core.config import CategoryBook, Settings
from core.classifier import Classifier
from core.organizer import Organizer, scan_folder, list_undo_files, undo
from core.media import ffmpeg_path, probe

PASS, FAIL = "  [OK] ", "  [!!] "
_errors: list[str] = []


def check(cond: bool, msg: str) -> None:
    print((PASS if cond else FAIL) + msg)
    if not cond:
        _errors.append(msg)


def md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
def make_samples(root: str) -> None:
    os.makedirs(root, exist_ok=True)
    sub = os.path.join(root, "子目录_旧照片")
    os.makedirs(sub, exist_ok=True)

    def photo(path, size, color, text):
        img = Image.new("RGB", size, color)
        d = ImageDraw.Draw(img)
        d.rectangle([size[0] // 4, size[1] // 4, size[0] * 3 // 4, size[1] * 3 // 4],
                    fill=(240, 220, 200))
        d.text((12, 12), text, fill=(20, 20, 20))
        img.save(path, quality=92)

    photo(os.path.join(root, "IMG_0001.jpg"), (1200, 900), (90, 130, 190), "sample A")
    photo(os.path.join(root, "IMG_0002.jpg"), (900, 1200), (170, 120, 90), "sample B")
    photo(os.path.join(root, "DSC_3311.JPG"), (1600, 1067), (110, 160, 110), "sample C")
    photo(os.path.join(root, "Screenshot_20210701.png"), (1920, 1080), (245, 245, 245), "shot")
    photo(os.path.join(root, "屏幕截图 2021-08-01 101010.png"), (1366, 768), (250, 250, 250), "shot2")
    photo(os.path.join(sub, "old_01.jpg"), (800, 600), (200, 100, 120), "in subdir")
    photo(os.path.join(sub, "old_02.jpg"), (800, 600), (100, 200, 160), "in subdir2")

    Image.new("RGB", (600, 400), (120, 120, 200)).save(os.path.join(root, "pic_webp.webp"))

    frames = []
    for i in range(6):
        im = Image.new("RGB", (320, 240), (30 * i, 90, 200 - 20 * i))
        ImageDraw.Draw(im).ellipse([40 + i * 20, 60, 120 + i * 20, 140], fill=(255, 230, 80))
        frames.append(im)
    frames[0].save(os.path.join(root, "funny.gif"), save_all=True,
                   append_images=frames[1:], duration=120, loop=0)

    exe = ffmpeg_path()
    if exe:
        for name, src in (("clip_a.mp4", "testsrc"), ("clip_b.mov", "smptebars")):
            out = os.path.join(root, name)
            subprocess.run(
                [exe, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", f"{src}=duration=3:size=640x480:rate=15", "-pix_fmt", "yuv420p", out],
                check=False, creationflags=0x08000000 if sys.platform == "win32" else 0,
            )


# --------------------------------------------------------------------------- #
def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        tmp = os.path.abspath(args[0])
    else:
        base = os.environ.get("PHOTO_SORTER_TESTDIR") or tempfile.gettempdir()
        tmp = os.path.join(base, "photo_sorter_selftest")
    shutil.rmtree(tmp, ignore_errors=True)
    root = os.path.join(tmp, "手机21年照片")

    print("\n=== 1. 生成样本 ===")
    make_samples(root)
    src_files = scan_folder(root, CategoryBook.load(), recursive=True)
    before = {p: (md5(p), os.path.getsize(p), os.path.getmtime(p)) for p in src_files}
    print(f"  样本 {len(src_files)} 个 -> {root}")
    check(len(src_files) >= 9, "样本数量足够")

    print("\n=== 2. 读取层（只读探测） ===")
    for p in src_files:
        info = probe(p)
        tag = "动图" if info.is_animated else info.kind
        print(f"    {os.path.basename(p):38s} {tag:6s} {info.width}x{info.height} "
              f"{'缩略图OK' if info.thumb else '无缩略图:' + info.error}")
    check(all(md5(p) == before[p][0] for p in src_files), "探测后原文件 MD5 全部未变")

    print("\n=== 3. 扫描规则 ===")
    book = CategoryBook.load()
    top = scan_folder(root, book, recursive=False)
    deep = scan_folder(root, book, recursive=True)
    check(len(deep) > len(top), f"递归({len(deep)}) 比 非递归({len(top)}) 多，子目录被正确纳入")

    print("\n=== 4. 识别 + 编号 ===")
    st = Settings(date_tag="", recursive=True, video_as_own_category=False)
    use_ai = "--no-ai" not in sys.argv and os.environ.get("PHOTO_SORTER_NOAI") != "1"
    clf = Classifier(book, use_ai=use_ai)
    ok, msg = clf.prepare(lambda m: print("    " + m))
    print(f"    语义引擎：{'启用' if ok else '未启用'} - {msg}")
    org = Organizer(book, st, clf)
    items = org.build_plan(root, deep)
    org.assign_names(root, items)
    for it in items:
        print(f"    {os.path.basename(it.src):38s} -> {it.category:6s} {it.new_name:28s} ({it.reason})")

    names = [it.new_name for it in items]
    check(len(set(names)) == len(names), "新文件名无重复")
    # 文件名结尾必须是「两位及以上序号 + 扩展名」；日期段可省略（纯序号）或带中杠（如 21-01）。
    # 对应实际格式：人像01.jpg / 人像21-01.jpg / 2021-人像/人像01.jpg
    check(all(re.search(r"(?:-\d{2,4}(?:-\d{2}){0,2})?-\d{2,}\.[A-Za-z0-9]+$|^\D*\d{2,}\.[A-Za-z0-9]+$", n) for n in names),
          "编号格式 类别-序号 正确（含手动日期段或纯序号）")
    gif = [it for it in items if it.src.endswith(".gif")]
    check(bool(gif) and gif[0].category == "动图", "GIF 被规则判为「动图」")
    shots = [it for it in items if "creenshot" in it.src or "屏幕截图" in it.src]
    check(all(it.category == "截图" for it in shots), "截图被规则正确识别")
    check(all(os.path.splitext(it.src)[1] == os.path.splitext(it.new_name)[1] for it in items),
          "扩展名原样保留，未被篡改")

    print("\n=== 5. 执行归档 ===")
    rep = org.execute(root, items)
    print(f"    成功 {rep.moved} / 跳过 {rep.skipped} / 失败 {rep.failed}")
    print(f"    分布：{rep.by_category}")
    check(rep.failed == 0, "无失败项")

    moved_now = []
    for dirpath, _, fns in os.walk(root):
        for fn in fns:
            if not fn.endswith(".json"):
                moved_now.append(os.path.join(dirpath, fn))
    after = {md5(p): p for p in moved_now}
    same = sum(1 for p, (h, _, _) in before.items() if h in after)
    check(same == len(before), f"全部 {len(before)} 个文件字节级完全一致（零重编码/零降质）")

    size_ok = all(os.path.getsize(after[h]) == sz for p, (h, sz, _) in before.items() if h in after)
    check(size_ok, "文件体积一字节未变")
    mt_ok = all(abs(os.path.getmtime(after[h]) - mt) < 2 for p, (h, _, mt) in before.items() if h in after)
    check(mt_ok, "修改时间原样保留")

    print("\n=== 6. 防覆盖 ===")
    cat_dirs = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))
                and not d.startswith(".")]
    print(f"    生成的类别文件夹：{cat_dirs}")
    probe_dir = None
    for d in cat_dirs:
        fs = os.listdir(os.path.join(root, d))
        if fs:
            probe_dir = (d, fs[0])
            break
    if probe_dir:
        d, fn = probe_dir
        dup = os.path.join(root, "追加进来的新照片.jpg")
        shutil.copy2(os.path.join(root, d, fn), dup)
        items2 = org.build_plan(root, [dup])
        org.assign_names(root, items2)
        newname = items2[0].new_name
        print(f"    再放一个同类文件进去 -> 分配到 {newname}")
        check(newname != fn, "重复运行时序号自动续接，不会撞已有文件")
        rep2 = org.execute(root, items2)
        check(rep2.failed == 0 and os.path.exists(os.path.join(root, d, newname)), "二次归档成功且未覆盖")
        check(os.path.exists(os.path.join(root, d, fn)), "原有文件仍然健在，没被覆盖")

    print("\n=== 7. 撤销还原 ===")
    us = list_undo_files(root)
    check(bool(us), f"存在撤销记录 {len(us)} 份")
    total_ok = 0
    for u in us:
        o, f, errs = undo(u)
        total_ok += o
        if errs:
            print("    " + "; ".join(errs[:3]))
    print(f"    还原 {total_ok} 个")
    back = [p for p in before if os.path.exists(p)]
    check(len(back) == len(before), f"全部 {len(before)} 个文件回到原始路径")
    check(all(md5(p) == before[p][0] for p in back), "还原后 MD5 依旧一致")

    print("\n" + "=" * 60)
    if _errors:
        print(f"未通过 {len(_errors)} 项：")
        for e in _errors:
            print("   - " + e)
    else:
        print("全部通过。")
    print(f"测试目录：{root}")
    print("=" * 60)
    return 1 if _errors else 0


if __name__ == "__main__":
    sys.exit(main())
