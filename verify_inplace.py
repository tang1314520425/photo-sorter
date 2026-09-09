# -*- coding: utf-8 -*-
r"""
verify_inplace.py —— 「原位重命名」外部核查脚本（给第三方复核用）

在 testdata/ 的一份临时副本上跑三条对照通道，逐条断言：
  通道 A：date_source=exif      + 粒度 year  → 按拍摄时间/文件名日期命名（2023.风景.01）
  通道 B：date_source=ctime     + 粒度 year  → 强制按文件创建时间命名（勾选该选项时的行为）
  通道 C：date_source=exif      + 粒度 ymd   → 日期粒度切到 年.月.日（2023.11.05.风景.01）

每条通道都会断言：
  1) 照片全部被重命名、无失败项
  2) 命名格式符合 年[.月.日].分类.序号，扩展名原样保留
  3) 已知样本落到了「预期年份」
  4) 文件没被移动（改名前后所在文件夹一致）、没新建子文件夹
  5) 改名后字节级完全一致（零重编码 / 零降质）
  6) 非照片文件（.docx/.txt/.csv…）文件名与内容均未被改动
  7) 已符合命名格式的样本被跳过（幂等，不会重复处理）
  8) 撤销后能完整还原（文件名 + MD5 回到最初）

用法：
    python verify_inplace.py                 # 默认启用语义引擎（需 CLIP 权重）
    python verify_inplace.py --no-ai         # 不加载 CLIP，只验逻辑（离线可跑）
    python verify_inplace.py --work D:\xxx   # 指定工作副本目录
    python verify_inplace.py --keep          # 结束后保留工作副本不删除

退出码：0 = 全部通过；1 = 有断言失败。
"""
import hashlib
import os
import shutil
import sys
import tempfile

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from core.config import ALL_EXTS, CategoryBook, Settings          # noqa: E402
from core.classifier import Classifier                            # noqa: E402
from core.renamer import InPlaceRenamer, list_disk_undo_files, undo_disk  # noqa: E402
from core.organizer import UNDO_DIR                               # noqa: E402

# --------------------------------------------------------------------------- #
# 预期数据（用于外部核对：这些样本的年份是人为写死进 EXIF / 文件名的）
# --------------------------------------------------------------------------- #
EXPECT_YEAR = {
    "IMG_20210315.jpg":              "2021",   # EXIF DateTimeOriginal=2021:03:15
    "IMG_20220720.jpg":              "2022",   # EXIF =2022:07:20
    "IMG_20231105.jpg":              "2023",   # EXIF =2023:11:05
    "Screenshot_20210701.png":       "2021",   # 无 EXIF，文件名含 20210701
    "屏幕截图 2021-08-01 101010.png": "2021",   # 无 EXIF，中文名含 2021-08-01
    "VID_20220815.mp4":              "2022",   # 视频，文件名含 20220815
    "clip_20210909.mov":             "2021",   # 视频，文件名含 20210909
}
# 已符合命名格式 → 应被整体跳过（幂等）
SKIP_FILES = {"2023.风景.01.jpg", "2021.截图.02.png"}
# 非照片 → 必须完全不动
NON_PHOTO = {"重要文档.docx.txt", "说明.txt", "数据.csv", "重要文档.docx"}

NAME_RE = r"^(\d{4}(?:\.\d{2}){0,2})\.([^.]+)\.(\d{2,})$"

RESULTS: list[tuple[bool, str]] = []


def check(cond: bool, msg: str) -> bool:
    RESULTS.append((bool(cond), msg))
    print(f"  [{'OK' if cond else '!!'}] {msg}")
    return bool(cond)


def pad(s: str, n: int) -> str:
    """按显示宽度补齐（中文算 2 格），让表格对齐。"""
    w = sum(2 if ord(c) > 127 else 1 for c in str(s))
    return str(s) + " " * max(1, n - w)


def md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def snapshot(root: str) -> dict[str, tuple[str, int]]:
    """记录目录下每个文件的 相对路径 -> (md5, size)。

    注意：跳过 .photo_sorter_undo 撤销记录目录 —— 那是程序自己产生的审计文件，
    撤销之后依然会保留（方便二次撤销），不属于用户内容，不能拿来比对。
    """
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != UNDO_DIR]
        for fn in fns:
            p = os.path.join(dp, fn)
            out[os.path.relpath(p, root)] = (md5(p), os.path.getsize(p))
    return out


def subdirs(root: str) -> set[str]:
    out = set()
    for dp, dns, _ in os.walk(root):
        for d in dns:
            if d == UNDO_DIR:
                continue
            out.add(os.path.relpath(os.path.join(dp, d), root))
    return out


# --------------------------------------------------------------------------- #
def run_pass(label: str, work: str, date_source: str, granularity: str,
             book: CategoryBook, clf: Classifier) -> None:
    print("\n" + "─" * 78)
    print(f"  {label}")
    print(f"  date_source={date_source}   粒度={granularity}   工作副本：{work}")
    print("─" * 78)

    before = snapshot(work)
    dirs_before = subdirs(work)
    nonphoto_before = {k: v for k, v in before.items()
                       if os.path.basename(k) in NON_PHOTO}

    st = Settings(recursive=True, video_as_own_category=False,
                  date_source=date_source, inplace_date_granularity=granularity)
    ren = InPlaceRenamer(book, st, clf)
    items = ren.build_plan([work])
    ren.assign_names(items)

    # ---- 打印改名对照表 ----
    print(f"\n  {'原文件名':<36} → {'新文件名':<28} {'分类':<8} 时间来源")
    print("  " + "-" * 84)
    for it in sorted(items, key=lambda x: x.src):
        src_dir = os.path.basename(os.path.dirname(it.src))
        print(f"  [{src_dir}]")
        print(f"    {pad(os.path.basename(it.src), 34)} → {pad(it.new_name, 26)} "
              f"{pad(it.category, 8)} {it.time_source}")
        _ = src_dir

    print()
    check(len(items) > 0, f"扫描到待处理照片 {len(items)} 个")

    # 幂等：已符合格式的样本不应出现在待处理列表里
    got_names = {os.path.basename(it.src) for it in items}
    check(not (got_names & SKIP_FILES),
          f"已重命名的样本被正确跳过（{', '.join(sorted(SKIP_FILES))}）")

    # 命名格式
    import re as _re
    ok_fmt = all(_re.match(NAME_RE, os.path.splitext(it.new_name)[0]) for it in items)
    check(ok_fmt, "命名格式符合 年[.月.日].分类.序号")

    ok_ext = all(os.path.splitext(it.src)[1].lower() == os.path.splitext(it.new_name)[1].lower()
                 for it in items)
    check(ok_ext, "扩展名原样保留，未被篡改")

    # 未移动（新名所在目录 == 原目录）
    ok_stay = all(os.path.dirname(it.src) == os.path.dirname(it.dst) for it in items)
    check(ok_stay, "所有文件留在原文件夹内，未被移动")

    # ---- 执行 ----
    rep = ren.execute(items)
    print(f"\n  执行结果：成功 {rep.moved} / 跳过 {rep.skipped} / 失败 {rep.failed}")
    check(rep.failed == 0, "无失败项")

    after = snapshot(work)
    dirs_after = subdirs(work)

    # 未建子文件夹
    check(dirs_before == dirs_after,
          f"未新建任何子文件夹（{len(dirs_before)} 个目录保持不变）")

    # 字节级一致（改名只是换名，内容一字节未动）
    by_hash_before = {v[0]: k for k, v in before.items()}
    after_hashes = {v[0] for v in after.values()}
    kept = [h for h in by_hash_before if h in after_hashes]
    check(len(kept) == len(before),
          f"全部 {len(before)} 个文件字节级完全一致（零重编码/零降质）")

    # 非照片未被改动
    nonphoto_after = {k: v for k, v in after.items() if os.path.basename(k) in NON_PHOTO}
    check(nonphoto_before == nonphoto_after,
          f"非照片文件完全未被改动（{len(NON_PHOTO)} 个：.docx/.txt/.csv）")

    # ---- 预期年份核对（只在 date_source=exif 且粒度=year 时严格校验）----
    if date_source == "exif" and granularity == "year":
        print("\n  预期年份核对：")
        all_ok = True
        for it in items:
            base = os.path.basename(it.src)
            if base in EXPECT_YEAR:
                stem = os.path.splitext(it.new_name)[0]
                yr = stem.split(".")[0]
                good = yr == EXPECT_YEAR[base]
                all_ok &= good
                print(f"    {pad(base, 30)} → {pad(it.new_name, 24)} "
                      f"期望年份 {EXPECT_YEAR[base]}  实际 {yr}  {'✓' if good else '✗'}")
        check(all_ok, "所有已知样本的年份与 EXIF/文件名一致")

    # ---- 撤销还原 ----
    ok_u, fail_u, errs = undo_disk([work])
    print(f"\n  撤销还原：成功 {ok_u} / 失败 {fail_u}")
    if errs:
        for e in errs[:3]:
            print("    " + e)
    check(fail_u == 0, "撤销无失败项")

    restored = snapshot(work)
    ok_names = set(restored) == set(before)
    ok_md5 = all(restored.get(k, ("",))[0] == v[0] for k, v in before.items())
    check(ok_names and ok_md5, f"撤销后 {len(before)} 个文件全部回到原始名且 MD5 一致")


# --------------------------------------------------------------------------- #
def main() -> int:
    argv = sys.argv[1:]
    use_ai = "--no-ai" not in argv
    keep = "--keep" in argv
    work_arg = None
    if "--work" in argv:
        i = argv.index("--work")
        if i + 1 < len(argv):
            work_arg = argv[i + 1]

    src = os.path.join(HERE, "testdata")
    if not os.path.isdir(src):
        print(f"[×] 找不到测试样本目录：{src}")
        print("    请先运行：python make_testdata.py")
        return 1

    print("=" * 78)
    print("  原位重命名 —— 外部核查脚本")
    print("=" * 78)

    book = CategoryBook.load()
    clf = Classifier(book, use_ai=use_ai)
    ok, msg = clf.prepare(lambda m: print("    " + m))
    print(f"\n  语义引擎：{'启用' if ok else '未启用'} - {msg}")
    print(f"  测试样本：{src}")

    # 工作副本父目录：
    #  · 默认用系统临时目录下「唯一」的子目录（tempfile.mkdtemp），绝不触碰用户真实目录；
    #  · 若显式传 --work，则该目录必须不存在或为空，否则拒绝——
    #    避免误删恰好含有 pass1_exif_year 这类子目录的真实目录。
    if work_arg:
        work_arg = os.path.abspath(work_arg)
        if os.path.exists(work_arg):
            if os.listdir(work_arg):
                print(f"[×] --work 指定的目录非空：{work_arg}")
                print("    为避免误删真实数据，必须传入空目录或不传（默认用系统临时目录）。")
                return 1
            tmp_parent = work_arg
        else:
            os.makedirs(work_arg, exist_ok=True)
            tmp_parent = work_arg
    else:
        tmp_parent = tempfile.mkdtemp(prefix="photo_sorter_verify_")

    passes = [
        ("通道 A：按拍摄时间命名（EXIF → 文件名 → 文件时间），日期粒度=年",
         "exif", "year"),
        ("通道 B：强制按文件创建时间命名（勾选该选项），日期粒度=年",
         "ctime", "year"),
        ("通道 C：按拍摄时间命名，日期粒度=年.月.日",
         "exif", "ymd"),
    ]

    made = []
    for idx, (label, ds, gran) in enumerate(passes, 1):
        work = os.path.join(tmp_parent, f"pass{idx}_{ds}_{gran}")
        if os.path.exists(work):
            print(f"[×] 工作副本已存在：{work}")
            print("    请先清理该目录，或换一个 --work 目录（脚本不会删除已有目录）。")
            return 1
        shutil.copytree(src, work)
        made.append(work)
        run_pass(label, work, ds, gran, book, clf)

    # ---- 汇总 ----
    print("\n" + "=" * 78)
    print("  核查结论")
    print("=" * 78)
    bad = [m for ok_, m in RESULTS if not ok_]
    print(f"  断言总数 {len(RESULTS)}    通过 {len(RESULTS) - len(bad)}    失败 {len(bad)}")
    if bad:
        print("\n  未通过的断言：")
        for m in bad:
            print(f"    [×] {m}")
        print("\n  结论：存在未通过项 ❌")
        rc = 1
    else:
        print("\n  结论：全部通过 ✅")
        rc = 0

    if keep:
        print("\n  工作副本已保留：")
        for p in made:
            print(f"    {p}")
    else:
        for p in made:
            shutil.rmtree(p, ignore_errors=True)
        print(f"\n  工作副本已清理（临时目录：{tmp_parent}）")

    return rc


if __name__ == "__main__":
    sys.exit(main())
