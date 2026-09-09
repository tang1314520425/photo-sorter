# -*- coding: utf-8 -*-
"""深度自检探针：逐条验证可疑缺陷是否真实存在。

用法：python bugprobe.py
不修改任何业务代码：只读业务源码做静态核查，并在 tempfile 自动创建的隔离临时目录里验证；
绝不会触碰用户的真实文件。
"""

from __future__ import annotations

import io
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = "✅", "❌"
results: list[tuple[str, bool, str]] = []


def check(no: str, desc: str, ok: bool, detail: str = "") -> None:
    results.append((no, ok, detail))
    print(f"{PASS if ok else FAIL} {no} {desc}" + (f"\n      → {detail}" if detail else ""))


# ══════════════════════════════════════════════════════════════════
# 1. Namer 续号正则：带日期段时能否续接已有编号
# ══════════════════════════════════════════════════════════════════
def probe_namer_continue():
    from core.organizer import Namer

    tmp = tempfile.mkdtemp(prefix="probe_namer_")
    try:
        # Namer 会把文件放到 root/类别/ 子目录，已有文件必须建在那儿
        cat = os.path.join(tmp, "人像")
        os.makedirs(cat, exist_ok=True)
        for i in (1, 2, 3):
            open(os.path.join(cat, f"人像2021-03-15-{i:02d}.jpg"), "wb").write(b"x")
        n = Namer(tmp, "", date_numbering=True, date_granularity="day")
        dst, fn = n.next_name("人像", os.path.join(tmp, "a.jpg"),
                              datetime(2021, 3, 15).timestamp())
        ok = fn == "人像2021-03-15-04.jpg"
        check("BUG-01", "Namer 带日期段时能续接已有编号（应为 -04）",
              ok, f"实际得到：{fn}")
    except Exception as e:
        check("BUG-01", "Namer 带日期段时能续接已有编号", False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
# 2. Namer 性能：目标文件夹已有很多文件时是否退化成 O(n^2)
# ══════════════════════════════════════════════════════════════════
def probe_namer_perf():
    from core.organizer import Namer

    tmp = tempfile.mkdtemp(prefix="probe_perf_")
    try:
        N_FILES = 500
        cat = os.path.join(tmp, "风景")
        os.makedirs(cat, exist_ok=True)
        for i in range(1, N_FILES + 1):
            open(os.path.join(cat, f"风景2021-03-15-{i:02d}.jpg"), "wb").write(b"x")
        n = Namer(tmp, "", date_numbering=True, date_granularity="day")
        t0 = time.perf_counter()
        dst, fn = n.next_name("风景", os.path.join(tmp, "new.jpg"),
                              datetime(2021, 3, 15).timestamp())
        dt = time.perf_counter() - t0
        ok = dt < 0.5 and fn == f"风景2021-03-15-{N_FILES+1}.jpg"
        check("BUG-02", f"Namer 在 {N_FILES} 个已有文件下正确续号且耗时 < 0.5s",
              ok, f"实际 {dt*1000:.1f} ms，得到 {fn}")
    except Exception as e:
        check("BUG-02", "Namer 续号性能", False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
# 3. 1920x1080 的普通照片会不会被误判成「截图」
# ══════════════════════════════════════════════════════════════════
def probe_screenshot_false_positive():
    from core.media import MediaInfo, probe
    from core.config import CategoryBook, Category
    from core.classifier import Classifier

    book = CategoryBook(fallback="其它", categories=[
        Category(name="截图", rule="screenshot", prompts=["a screenshot"]),
        Category(name="风景", prompts=["a landscape photo"]),
    ])
    clf = Classifier(book, use_ai=False)

    tmp = tempfile.mkdtemp(prefix="probe_shot_")
    try:
        # 一张 1920x1080 的普通照片，文件名不带任何截图字样，且「有 EXIF」
        from PIL import Image
        p = os.path.join(tmp, "DSC_09876.jpg")
        img = Image.new("RGB", (1920, 1080), (90, 140, 200))
        exif = img.getexif()
        exif[36867] = "2023:05:20 14:30:00"
        img.save(p, exif=exif)

        info = probe(p)
        res = clf.classify(info, min_conf=0.0)
        ok = res.category != "截图"
        check("BUG-03", "1920×1080 带 EXIF 的普通照片不应被判成截图",
              ok, f"实际分类：{res.category}（{res.reason}）")
    except Exception as e:
        check("BUG-03", "截图误判探测", False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
# 4. RAW 文件是否读 EXIF 拍摄时间
# ══════════════════════════════════════════════════════════════════
def probe_raw_exif():
    from core.media import probe

    tmp = tempfile.mkdtemp(prefix="probe_raw_")
    try:
        # 造一个「伪 RAW」：内容是 TIFF（exifread 能读），扩展名 .dng
        # （.dng 本质是 TIFF，真实场景中 exifread 完全可以读出 DateTimeOriginal）
        from PIL import Image
        p = os.path.join(tmp, "camera_photo.dng")
        img = Image.new("RGB", (64, 48), (10, 20, 30))
        exif = img.getexif()
        exif[36867] = "2019:07:04 09:08:00"
        img.save(p, format="TIFF", exif=exif)
        os.utime(p, (time.time(), time.time()))   # 文件时间 = 现在

        info = probe(p)
        year = datetime.fromtimestamp(info.shot_time).year if info.shot_time else 0
        ok = year == 2019
        check("BUG-04", "RAW(.dng) 应读出 EXIF 拍摄年份 2019，而非文件时间",
              ok, f"实际年份={year}，time_source={info.time_source}，error={info.error}")
    except Exception as e:
        check("BUG-04", "RAW EXIF 探测", False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
# 5. 原位重命名是否会篡改扩展名大小写
# ══════════════════════════════════════════════════════════════════
def probe_ext_case():
    from core.renamer import InPlaceNamer

    tmp = tempfile.mkdtemp(prefix="probe_ext_")
    try:
        n = InPlaceNamer("year")
        dst, fn = n.next_name(tmp, "风景", os.path.join(tmp, "IMG_0001.JPG"),
                              datetime(2023, 1, 1).timestamp())
        ok = fn.endswith(".JPG")
        check("BUG-05", "扩展名大小写应保持不变（.JPG 不应变 .jpg）",
              ok, f"实际得到：{fn}")
    except Exception as e:
        check("BUG-05", "扩展名大小写", False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
# 6. 极端日期（1970 年前）会不会让 probe 崩掉
# ══════════════════════════════════════════════════════════════════
def probe_extreme_date():
    from core.media import probe

    tmp = tempfile.mkdtemp(prefix="probe_date_")
    try:
        from PIL import Image
        p = os.path.join(tmp, "IMG_19650101.jpg")
        Image.new("RGB", (32, 32), (1, 2, 3)).save(p)
        info = probe(p)
        crashed = "timestamp" in (info.error or "") or "OSError" in (info.error or "")
        check("BUG-06", "文件名含 1970 年前日期（1965）不应导致探测失败",
              not crashed, f"error={info.error!r}，year="
              f"{datetime.fromtimestamp(info.shot_time).year if info.shot_time else 0}")

        # 超远未来 / 非法月份
        p2 = os.path.join(tmp, "IMG_99991232.jpg")
        Image.new("RGB", (32, 32), (1, 2, 3)).save(p2)
        info2 = probe(p2)
        crashed2 = "OSError" in (info2.error or "") or "ValueError" in (info2.error or "")
        check("BUG-07", "非法日期（9999-12-32）不应导致探测失败",
              not crashed2, f"error={info2.error!r}")
    except Exception as e:
        check("BUG-06", "极端日期探测", False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
# 8. 原位重命名 _uniq 冲突后缀是否破坏幂等格式
# ══════════════════════════════════════════════════════════════════
def probe_uniq_format():
    from core.renamer import InPlaceNamer, _DONE_RE

    tmp = tempfile.mkdtemp(prefix="probe_uniq_")
    try:
        open(os.path.join(tmp, "2023.风景.01.jpg"), "wb").write(b"x")
        n = InPlaceNamer("year")
        dst, fn = n.next_name(tmp, "风景", os.path.join(tmp, "a.jpg"),
                              datetime(2023, 1, 1).timestamp())
        stem = os.path.splitext(fn)[0]
        ok = bool(_DONE_RE.match(stem))
        check("BUG-08", "冲突加后缀后仍应符合「年.分类.序号」格式（可幂等跳过）",
              ok, f"实际得到：{fn}（stem={stem!r}）")
    except Exception as e:
        check("BUG-08", "冲突后缀格式", False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
# 9. InPlaceNamer 性能：每次 next_name 都 listdir → O(n^2)
# ══════════════════════════════════════════════════════════════════
def probe_inplace_perf():
    from core.renamer import InPlaceNamer

    tmp = tempfile.mkdtemp(prefix="probe_ipperf_")
    try:
        N = 400
        for i in range(1, N + 1):
            open(os.path.join(tmp, f"2023.风景.{i:02d}.jpg"), "wb").write(b"x")
        n = InPlaceNamer("year")
        # 模拟一次批量：连续发 N 个号
        t0 = time.perf_counter()
        for _ in range(200):
            n.next_name(tmp, "风景", os.path.join(tmp, "z.jpg"),
                        datetime(2023, 1, 1).timestamp())
        dt = time.perf_counter() - t0
        ok = dt < 1.0
        check("BUG-09", "InPlaceNamer 连续发 200 个号（目录 400 文件）耗时 < 1s",
              ok, f"实际 {dt*1000:.0f} ms")
    except Exception as e:
        check("BUG-09", "InPlaceNamer 性能", False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
# 10. Settings 字段类型被写坏时会不会崩
# ══════════════════════════════════════════════════════════════════
def probe_settings_type():
    from core.config import Settings
    import json, tempfile

    tmp = tempfile.mkdtemp(prefix="probe_set_")
    try:
        p = os.path.join(tmp, "settings.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"min_confidence": "0.5", "mode": 123,
                       "scan_roots": "D:\\\\", "recursive": "yes"}, f)
        try:
            s = Settings.load(p)
            # 类型错了但不崩；看会不会在后续使用中炸
            bad = []
            if not isinstance(s.min_confidence, float):
                bad.append(f"min_confidence={s.min_confidence!r}(应为float)")
            if not isinstance(s.scan_roots, list):
                bad.append(f"scan_roots={s.scan_roots!r}(应为list)")
            check("BUG-10", "Settings 读到类型错误的字段时应自动纠正而非原样吞下",
                  not bad, "；".join(bad) if bad else "类型已自动纠正")
        except Exception as e:
            check("BUG-10", "Settings 类型容错", False, f"直接抛异常：{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
# 11. 扫描根目录是否会闯进 Windows / Program Files 等系统目录
# ══════════════════════════════════════════════════════════════════
def probe_system_dirs():
    from core.organizer import should_skip_dir
    # 用文件系统上的真实大小写来测（回收站实际叫 $Recycle.Bin，不是 $RECYCLE.BIN）
    danger = ["Windows", "Program Files", "Program Files (x86)", "ProgramData",
              "AppData", "$Recycle.Bin", "System32", "node_modules", ".venv",
              "venv", "WinSxS", "$RECYCLE.BIN", "__pycache__"]
    missing = [d for d in danger if not should_skip_dir(d)]
    check("BUG-11", "扫描剪枝应覆盖常见系统/依赖目录（且大小写不敏感）",
          not missing, f"未覆盖：{missing}" if missing else
          f"已覆盖 {len(danger)} 个目录名")


# ══════════════════════════════════════════════════════════════════
# 12. undo_disk 会不会误撤其它模式（organize）留下的记录
# ══════════════════════════════════════════════════════════════════
def probe_undo_cross_mode():
    """功能性验证：同一个根目录里既有 organize 记录、又有 inplace 记录时，
    在 inplace 模式下取撤销列表，不应该把 organize 那条也捞出来。"""
    from core.organizer import Organizer
    from core.renamer import list_disk_undo_files, INPLACE_KIND

    tmp = tempfile.mkdtemp(prefix="probe_undo_")
    try:
        Organizer._write_undo(tmp, [{"src": "a", "dst": "b"}], kind="organize")
        Organizer._write_undo(tmp, [{"src": "c", "dst": "d"}], kind=INPLACE_KIND)
        allf = list_disk_undo_files([tmp], kind=None)
        only_inplace = list_disk_undo_files([tmp], kind=INPLACE_KIND)
        ok = len(allf) == 2 and len(only_inplace) == 1
        check("BUG-12", "inplace 模式撤销只取原位重命名的记录（不误撤 organize 历史）",
              ok, f"全部记录 {len(allf)} 条，按 inplace 过滤后 {len(only_inplace)} 条"
                  f"（期望 2 / 1）")

        # 陈旧记录（无 kind 字段）应被归到 organize，不会被 inplace 撤掉
        import json
        d = os.path.join(tmp, ".photo_sorter_undo")
        legacy = os.path.join(d, "undo_20260101_000000_000000.json")
        with open(legacy, "w", encoding="utf-8") as f:
            json.dump({"time": 0, "root": tmp, "moves": [{"src": "x", "dst": "y"}]}, f)
        after = list_disk_undo_files([tmp], kind=INPLACE_KIND)
        ok2 = len(after) == 1
        check("BUG-12b", "旧版本遗留的无 kind 记录不应被 inplace 撤销捞走",
              ok2, f"加入旧记录后 inplace 仍为 {len(after)} 条（期望 1）")
    except Exception as e:
        check("BUG-12", "撤销跨模式隔离", False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
# 13. date_segment / _inplace_date 对非法时间戳是否健壮
# ══════════════════════════════════════════════════════════════════
def probe_date_robust():
    from core.organizer import date_segment
    from core.renamer import _inplace_date
    bad = []
    for ts in (-1.0, 1.0, 253402300800.0, -2208988800.0, 99999999999.0):
        try:
            date_segment(ts, "day")
        except Exception as e:
            bad.append(f"date_segment({ts}) → {type(e).__name__}: {e}")
        try:
            _inplace_date(ts, "ymd")
        except Exception as e:
            bad.append(f"_inplace_date({ts}) → {type(e).__name__}: {e}")
    check("BUG-13", "日期转换应对越界时间戳健壮（不抛异常）",
          not bad, "；".join(bad) if bad else "已健壮")


# ══════════════════════════════════════════════════════════════════
# 14. _read_raw 中 img 可能未定义 → UnboundLocalError
# ══════════════════════════════════════════════════════════════════
def probe_raw_unbound():
    src = ""
    try:
        with open("core/media.py", encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        check("BUG-14", "raw 分支", False, str(e))
        return
    seg = src[src.find("def _read_raw"):src.find("def _read_raw") + 1800]
    has_guard = "img = None" in seg or "UnboundLocal" in seg
    check("BUG-14", "_read_raw 中 img 应有兜底定义（防 UnboundLocalError）",
          has_guard, "extract_thumb 与 postprocess 都失败时 img 未定义，第 220 行会抛 UnboundLocalError"
          if not has_guard else "已有兜底")


# ══════════════════════════════════════════════════════════════════
# 15. 同目录大小写不同名的冲突（Windows 不区分大小写）
# ══════════════════════════════════════════════════════════════════
def probe_case_collision():
    """原位改名的核心硬要求：目标文件已存在时，_safe_move 必须「跳过」而非「覆盖」，
    绝不允许破坏已有文件。该探针在隔离临时目录里验证这一跨平台要求，
    不依赖具体文件系统是否区分大小写（大小写敏感/不敏感文件系统结论一致）。"""
    from core.organizer import Organizer
    tmp = tempfile.mkdtemp(prefix="probe_case_")
    try:
        # 场景 A：目标不存在 → 正常改名应成功，且内容、源文件状态正确
        src = os.path.join(tmp, "IMG_A.JPG")
        open(src, "wb").write(b"dataA")
        dst = os.path.join(tmp, "2023.其它.01.jpg")
        Organizer._safe_move(src, dst)
        ok_a = (os.path.exists(dst) and open(dst, "rb").read() == b"dataA"
                and not os.path.exists(src))
        check("BUG-15a", "目标不存在时正常改名成功", ok_a)

        # 场景 B：目标已被另一个不同文件占用 → 必须跳过，绝不覆盖
        src2 = os.path.join(tmp, "IMG_B.JPG")
        open(src2, "wb").write(b"dataB")
        dst2 = os.path.join(tmp, "2023.其它.01.jpg")  # 场景 A 已生成
        try:
            Organizer._safe_move(src2, dst2)
            overwrote = open(dst2, "rb").read() == b"dataB"
            check("BUG-15b", "目标已存在时必须跳过而非覆盖",
                  not overwrote, "目标文件被覆盖！" if overwrote else "已正确跳过（未覆盖）")
        except Exception:
            check("BUG-15b", "目标已存在时必须跳过而非覆盖（被安全拒绝）", True, "已拒绝")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    print("=" * 70)
    print("photo_sorter 深度自检探针")
    print("=" * 70)
    probes = [
        probe_namer_continue, probe_namer_perf, probe_screenshot_false_positive,
        probe_raw_exif, probe_ext_case, probe_extreme_date, probe_uniq_format,
        probe_inplace_perf, probe_settings_type, probe_system_dirs,
        probe_undo_cross_mode, probe_date_robust, probe_raw_unbound,
        probe_case_collision,
    ]
    for p in probes:
        try:
            p()
        except Exception as e:
            print(f"❌ {p.__name__} 探针自身异常：{type(e).__name__}: {e}")
            results.append((p.__name__, False, str(e)))
    bad = [r for r in results if not r[1]]
    print("=" * 70)
    print(f"检出问题 {len(bad)} 项 / 共 {len(results)} 项检查")
    if bad:
        print("\n【确认存在的缺陷】")
        for no, _, d in bad:
            print(f"  ✗ {no}: {d}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
