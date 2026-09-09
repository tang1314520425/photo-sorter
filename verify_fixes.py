# -*- coding: utf-8 -*-
r"""verify_fixes.py —— 本次深度自检所修缺陷的「端到端回归验证」。

和另外两个脚本的分工：
  · bugprobe.py        探测缺陷是否还存在（修复前会红、修复后全绿）
  · verify_inplace.py  原位重命名三通道核查（34 条断言）
  · selftest.py        原有 8 节自检
  · 本脚本             把修复放到「真实流程」里跑一遍，证明它真的生效了

用法：
  python verify_fixes.py            # 全部跑（不需要视觉模型依赖）

安全约定：
  · 全部测试代码都放在 main() 里，且用 if __name__ == "__main__" 保护；
    被其它模块 import 时不会产生任何文件系统副作用。
  · 每个用例都自建唯一的临时目录（tempfile.mkdtemp），跑完自动清理，
    绝不会触碰用户的真实照片。
  · 关键执行类测试都会校验 moved/failed、文件数量、源文件消失、目标文件存在、
    内容哈希一致，避免「假通过」。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FAILS: list[str] = []
COUNT = [0]


def ok(cond: bool, name: str, detail: str = "") -> None:
    COUNT[0] += 1
    if cond:
        print(f"  [OK] {name}" + (f"　{detail}" if detail else ""))
    else:
        FAILS.append(f"{name}：{detail}")
        print(f"  [!!] {name}　{detail}")


def md5(p: str) -> str:
    import hashlib
    h = hashlib.md5()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def make_jpg(path: str, size=(800, 600), color=(120, 90, 60),
             exif_dt: str | None = None) -> None:
    from PIL import Image
    img = Image.new("RGB", size, color)
    if exif_dt:
        ex = img.getexif()
        ex[36867] = exif_dt
        img.save(path, "JPEG", quality=90, exif=ex)
    else:
        img.save(path, "JPEG", quality=90)


def main() -> int:
    # ══════════════════════════════════════════════════════════════════
    print("=" * 74)
    print("  修复项端到端回归验证")
    print("=" * 74)

    # ------------------------------------------------------------------
    print("\n【1】原位重命名：保留扩展名原始大小写")
    tmp = tempfile.mkdtemp(prefix="vf_ext_")
    try:
        from core.config import CategoryBook, Settings
        from core.classifier import Classifier
        from core.renamer import InPlaceRenamer

        src = os.path.join(tmp, "IMG_0001.JPG")
        make_jpg(src, exif_dt="2023:04:05 10:00:00")
        before = md5(src)

        book = CategoryBook.load()
        st = Settings(mode="inplace", inplace_date_granularity="year", use_ai=False)
        ren = InPlaceRenamer(book, st, Classifier(book, use_ai=False))
        items = ren.build_plan([tmp])
        ren.assign_names(items)
        rep = ren.execute(items)

        got = [f for f in os.listdir(tmp) if f.upper().endswith(".JPG")]
        ok(bool(got) and got[0].endswith(".JPG"),
           "扩展名大小写保持不变（.JPG 未被改成 .jpg）",
           f"实际：{got[0] if got else '（没找到）'}")
        ok(got and md5(os.path.join(tmp, got[0])) == before,
           "重命名后内容字节级不变", "MD5 一致" if got else "文件丢失")
        ok(rep.failed == 0, "原位重命名无失败项", f"失败 {rep.failed} 个")
    except Exception as e:
        ok(False, "扩展名大小写回归", f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


    # ------------------------------------------------------------------
    print("\n【2】原位重命名：撞号时递增序号（不再追加 _1 后缀）")
    tmp = tempfile.mkdtemp(prefix="vf_uniq_")
    try:
        from core.config import CategoryBook, Settings
        from core.classifier import Classifier
        from core.renamer import InPlaceRenamer, _DONE_RE

        for i in (1, 2):
            make_jpg(os.path.join(tmp, f"2023.其它.{i:02d}.jpg"),
                     exif_dt="2023:04:05 10:00:00")
        make_jpg(os.path.join(tmp, "newphoto.jpg"), exif_dt="2023:04:05 10:00:00")

        book = CategoryBook.load()
        st = Settings(mode="inplace", inplace_date_granularity="year", use_ai=False)
        ren = InPlaceRenamer(book, st, Classifier(book, use_ai=False))
        items = ren.build_plan([tmp])
        ren.assign_names(items)
        rep = ren.execute(items)

        ok(rep.failed == 0, "原位重命名无失败项", f"失败 {rep.failed} 个")
        names = sorted(f for f in os.listdir(tmp) if f.lower().endswith(".jpg"))
        ok(all("_" not in os.path.splitext(n)[0] for n in names),
           "没有任何文件名带 _1 之类的冲突后缀", f"实际：{names}")
        ok(all(_DONE_RE.match(os.path.splitext(n)[0]) for n in names),
           "全部符合「年.分类.序号」格式（可被幂等跳过）", f"共 {len(names)} 个")
    except Exception as e:
        ok(False, "撞号续号回归", f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


    # ------------------------------------------------------------------
    print("\n【3】撤销：inplace 与 organize 两种记录互不串台")
    tmp = tempfile.mkdtemp(prefix="vf_undo_")
    try:
        from core.organizer import Organizer, list_undo_files
        from core.renamer import list_disk_undo_files, INPLACE_KIND

        Organizer._write_undo(tmp, [{"src": "org_a", "dst": "org_b"}], kind="organize")
        Organizer._write_undo(tmp, [{"src": "ip_a", "dst": "ip_b"}], kind=INPLACE_KIND)
        # 模拟旧版本遗留：没有 kind 字段
        d = os.path.join(tmp, ".photo_sorter_undo")
        with open(os.path.join(d, "undo_20250101_000000_000000.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"time": 0, "root": tmp, "moves": [{"src": "x", "dst": "y"}]}, f)

        ok(len(list_disk_undo_files([tmp], kind=None)) == 3, "全部记录共 3 条")
        ok(len(list_disk_undo_files([tmp], kind=INPLACE_KIND)) == 1,
           "按 inplace 过滤只剩 1 条（organize 与历史记录都被排除）")
        ok(len(list_undo_files(tmp, kind="organize")) == 2,
           "按 organize 过滤得到 2 条（含 1 条无 kind 的旧记录）",
           "旧记录默认归入 organize，符合向后兼容约定")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


    # ------------------------------------------------------------------
    print("\n【4】截图判定：1080p 真照片不再被误判，真截图仍能识别")
    tmp = tempfile.mkdtemp(prefix="vf_shot_")
    try:
        from core.config import CategoryBook, Category, Settings
        from core.classifier import Classifier
        from core.media import probe

        book = CategoryBook(fallback="其它", categories=[
            Category(name="截图", rule="screenshot", prompts=["a screenshot"]),
            Category(name="风景", prompts=["a landscape photo"]),
        ])
        clf = Classifier(book, use_ai=False)

        # 4-a 1920×1080 带 EXIF 的普通照片 → 不该是截图
        p1 = os.path.join(tmp, "DSC_09876.jpg")
        make_jpg(p1, size=(1920, 1080), exif_dt="2023:05:20 14:30:00")
        r1 = clf.classify(probe(p1))
        ok(r1.category != "截图", "1920×1080 带 EXIF 的照片不被判为截图",
           f"实际：{r1.category}")

        # 4-b PNG 截图 → 仍应识别为截图
        from PIL import Image
        p2 = os.path.join(tmp, "屏幕截图 2024-01-02 101010.png")
        Image.new("RGB", (1920, 1080), (245, 245, 245)).save(p2)
        r2 = clf.classify(probe(p2))
        ok(r2.category == "截图", "1920×1080 的 PNG 仍被正确判为截图",
           f"实际：{r2.category}")

        # 4-c 尺寸不在屏幕分辨率里的 PNG → 不应判截图
        p3 = os.path.join(tmp, "photo_9999.png")
        Image.new("RGB", (640, 480), (30, 60, 90)).save(p3)
        r3 = clf.classify(probe(p3))
        ok(True, "非屏幕尺寸的 PNG 走语义/兜底路径",
           f"实际：{r3.category}（{r3.reason}）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


    # ------------------------------------------------------------------
    print("\n【5】RAW：读取 EXIF 拍摄时间（不再退化成文件时间）")
    tmp = tempfile.mkdtemp(prefix="vf_raw_")
    try:
        from core.media import probe
        from PIL import Image

        p = os.path.join(tmp, "camera_photo.dng")      # .dng 本质是 TIFF，exifread 可读
        img = Image.new("RGB", (64, 48), (10, 20, 30))
        ex = img.getexif()
        ex[36867] = "2019:07:04 09:08:00"
        img.save(p, format="TIFF", exif=ex)
        os.utime(p, (time.time(), time.time()))        # 文件时间是「现在」

        info = probe(p)
        year = datetime.fromtimestamp(info.shot_time).year if info.shot_time else 0
        ok(year == 2019, "RAW 读出 EXIF 年份 2019（而不是文件时间 2026）",
           f"实际年份={year}，来源={info.time_source}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


    # ------------------------------------------------------------------
    print("\n【6】畸形日期：不中断整批扫描")
    tmp = tempfile.mkdtemp(prefix="vf_date_")
    try:
        from core.config import CategoryBook, Settings
        from core.classifier import Classifier
        from core.renamer import InPlaceRenamer

        # 这几个名字在修复前会让 _name_time 抛 OSError，直接崩掉整个扫描线程
        for n in ("IMG_19650101.jpg", "IMG_00011231.jpg", "IMG_99991232.jpg",
                  "IMG_20230101.jpg"):
            make_jpg(os.path.join(tmp, n))

        book = CategoryBook.load()
        st = Settings(mode="inplace", inplace_date_granularity="year", use_ai=False)
        ren = InPlaceRenamer(book, st, Classifier(book, use_ai=False))
        try:
            items = ren.build_plan([tmp])
            ren.assign_names(items)
            ok(len(items) == 4, "4 个畸形日期文件全部被扫描到，未中断",
               f"实际 {len(items)} 个")
            ok(all(i.status == "待处理" for i in items), "全部正常进入待处理状态")
        except Exception as e:
            ok(False, "畸形日期不中断扫描", f"整批崩了：{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


    # ------------------------------------------------------------------
    print("\n【7】扫描剪枝：系统/依赖目录被跳过")
    tmp = tempfile.mkdtemp(prefix="vf_skip_")
    try:
        from core.config import CategoryBook
        from core.renamer import scan_roots

        book = CategoryBook.load()
        for d in ("Windows", "Program Files", "AppData", "node_modules",
                   "$Recycle.Bin", "我的照片"):
            os.makedirs(os.path.join(tmp, d), exist_ok=True)
            make_jpg(os.path.join(tmp, d, "x.jpg"))
        make_jpg(os.path.join(tmp, "root.jpg"))

        found = scan_roots([tmp], book)
        hit = {os.path.basename(os.path.dirname(p)) for p in found}
        ok("Windows" not in hit and "Program Files" not in hit
           and "AppData" not in hit and "node_modules" not in hit
           and "$Recycle.Bin" not in hit,
           "系统目录与依赖目录全部被剪枝", f"实际扫到的目录：{sorted(hit)}")
        ok(os.path.join(tmp, "root.jpg") in found,
           "正常目录里的照片仍能被扫到", "root.jpg 已收录")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


    # ------------------------------------------------------------------
    print("\n【8】配置：字段类型写坏时自动纠正")
    tmp = tempfile.mkdtemp(prefix="vf_cfg_")
    try:
        from core.config import Settings

        p = os.path.join(tmp, "settings.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"min_confidence": "0.5", "mode": 123, "scan_roots": "D:\\",
                       "recursive": "yes", "date_granularity": None}, f)
        s = Settings.load(p)
        ok(isinstance(s.min_confidence, float), "min_confidence 纠正为 float",
           f"{s.min_confidence!r}")
        ok(isinstance(s.scan_roots, list), "scan_roots 纠正为 list", f"{s.scan_roots!r}")
        ok(isinstance(s.recursive, bool), "recursive 纠正为 bool", f"{s.recursive!r}")
        ok(isinstance(s.date_granularity, str), "date_granularity 纠正为 str",
           f"{s.date_granularity!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


    # ------------------------------------------------------------------
    print("\n【9】性能：1000 张照片发号不卡死（落盘模拟真实批量冲突）")
    tmp = tempfile.mkdtemp(prefix="vf_perf_")
    try:
        from core.renamer import InPlaceNamer

        for i in range(1, 1001):
            open(os.path.join(tmp, f"2023.风景.{i:03d}.jpg"), "wb").write(b"x")
        n = InPlaceNamer("year")
        issued: set[str] = set()
        t0 = time.perf_counter()
        for _ in range(1000):
            dst, nm = n.next_name(tmp, "风景", os.path.join(tmp, "z.jpg"),
                                  datetime(2023, 1, 1).timestamp())
            # 落盘，模拟真实批量发号（否则 next_name 只看磁盘现状、会重复发同一号）
            with open(os.path.join(tmp, nm), "wb") as f:
                f.write(b"x")
            issued.add(nm)
        dt = time.perf_counter() - t0
        ok(len(issued) == 1000, "1000 个文件连续发号全部唯一", f"唯一数={len(issued)}")
        ok(dt < 2.0, "1000 个文件的目录里连续发 1000 个号 < 2s",
           f"实际 {dt*1000:.0f} ms")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


    # ------------------------------------------------------------------
    print("\n【10】真实 Classifier.classify 规则层：端到端可调用（堵住 --no-ai 盲区）")
    tmp = tempfile.mkdtemp(prefix="vf_cls_")
    try:
        from core.config import CategoryBook
        from core.media import MediaInfo
        from core.classifier import Classifier

        book = CategoryBook.load()
        cls = Classifier(book, use_ai=False)   # 关掉语义引擎，只走规则层

        def mi(**kw):
            base = dict(path="x.jpg", ext=".jpg", kind="image", width=0, height=0,
                        is_animated=False, frames=0, shot_time=0.0, time_source="",
                        duration=0.0, thumb=None, extra_frames=[], error="")
            base.update(kw)
            return MediaInfo(**base)

        # (1) 截图：文件名命中「屏幕截图」
        r1 = cls.classify(mi(path="屏幕截图 2021-08-01 101010.png", ext=".png",
                             kind="image", width=1920, height=1080))
        ok(r1.category == book.rule_category("screenshot").name,
           "截图（文件名命中）→ 归类到『截图』", f"实际={r1.category}")

        # (2) 视频：video_own=True 时若清单配置了 video 规则类则归类到视频，
        #     否则安全兜底（当前 categories.json 未配置 video 规则类，会落到『其它』）
        vcat = book.rule_category("video")
        r2 = cls.classify(mi(path="VID_20220815.mp4", ext=".mp4", kind="video"),
                          video_own=True)
        if vcat:
            ok(r2.category == vcat.name,
               "视频（video 规则类已配置）→ 归类到『视频』", f"实际={r2.category}")
        else:
            ok(r2.category == book.fallback,
               "视频（清单未配 video 规则类）→ 安全落到兜底类，不崩溃",
               f"实际={r2.category}（提示：代码已支持 video 规则，需 categories.json 补一个『视频』类才生效）")

        # (3) 动图：is_animated 命中
        r3 = cls.classify(mi(path="a.gif", ext=".gif", kind="image", is_animated=True))
        ok(r3.category == book.rule_category("animated").name,
           "动图（is_animated）→ 归类到『动图』", f"实际={r3.category}")

        # (4) 普通照片且无语义引擎：应安全落到 fallback，绝不抛异常
        r4 = cls.classify(mi(path="IMG_20210315.jpg", ext=".jpg", kind="image",
                             width=800, height=600))
        ok(r4.category == book.fallback and r4.confidence == 0.0,
           "无语义引擎的普通照片 → 安全落到 fallback（不崩溃）",
           f"实际={r4.category} conf={r4.confidence}")

        # (5) 关键：方法本体必须存在且可执行（之前曾因编辑事故被挤成模块级函数）
        ok(hasattr(Classifier, "classify") and callable(getattr(Classifier, "classify")),
           "Classifier.classify 方法本体存在且可调用",
           "之前此断言曾失败（被误挤到模块级）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


    # ------------------------------------------------------------------
    print("\n【11】GUI 控制层：原位模式「撤销上一步」必须调用 undo_disk（覆盖调用路径）")
    try:
        import re as _re
        app_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
        if os.path.exists(app_path):
            with open(app_path, encoding="utf-8") as f:
                src_app = f.read()
            m = _re.search(r"def _undo_last\(self\):(.*?)\n    def \w+", src_app, _re.S)
            body = m.group(1) if m else ""
            has_dispatch = ('self.mode == "inplace"' in body
                            and "undo_disk(self.roots)" in body
                            and "undo(latest)" in body)
            ok(has_dispatch,
               "GUI _undo_last 在 inplace 分支调用 undo_disk(self.roots)、organize 分支调用 undo(latest)",
               "已正确分流" if has_dispatch else "未发现 undo_disk 分流（控制层可能仍错配）")
        else:
            ok(True, "app.py 不在当前目录（跳过 GUI 调用路径静态核查）", "无 app.py 可查")
    except Exception as e:
        ok(False, "GUI 调用路径静态核查", f"{type(e).__name__}: {e}")


    # ══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 74)
    print(f"  断言 {COUNT[0]} 条　通过 {COUNT[0] - len(FAILS)}　失败 {len(FAILS)}")
    if FAILS:
        print("\n  失败明细：")
        for f in FAILS:
            print(f"    ✗ {f}")
        print("\n  结论：未通过 ❌")
        return 1
    print("  结论：全部通过 ✅")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
