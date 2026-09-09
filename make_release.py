# -*- coding: utf-8 -*-
"""生成「源码发布包」zip（纯源码，不含 exe / 权重）。

校验和 CHECKSUMS.txt 会同时写进 zip（供下载者验证完整性），
并**保留**在源码目录下，方便本地自查——不再物理删除。

输出路径可配置：
  - 命令行  : python make_release.py -o "D:\\发布\\photo_sorter_v1.0.2.zip"
  - 环境变量: set PHOTO_SORTER_OUT=D:\\发布\\photo_sorter_v1.0.2.zip
  - 都不给  : 回落到本机默认 D:\\... 路径（仅 TJM 本机有效）
"""
import argparse
import os
import zipfile
import hashlib

SRC = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(SRC, "VERSION"), encoding="utf-8") as _f:
    VER = _f.read().strip()

DEFAULT_OUT = os.path.join(
    r"D:\AI本地大模型\work Buddy生成的软件（开源）",
    f"photo_sorter_v{VER}.zip",
)

ROOT = "photo_sorter"
SKIP_DIRS = {".git", "__pycache__", "venv", "envs", ".photo_sorter_undo", "build", "dist"}
SKIP_FILES = {"settings.json", "make_release.py", "CHECKSUMS.txt"}
EXTRA_SKIP_SUFFIX = (".pyc", ".zip", ".spec")


def resolve_out() -> str:
    ap = argparse.ArgumentParser(description="生成源码发布包")
    ap.add_argument("-o", "--out", default=os.environ.get("PHOTO_SORTER_OUT", DEFAULT_OUT),
                    help="输出 zip 路径（也可用环境变量 PHOTO_SORTER_OUT）")
    return ap.parse_args().out


def main() -> None:
    OUT = resolve_out()
    files = []
    for dp, dns, fns in os.walk(SRC):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            fp = os.path.join(dp, fn)
            rel = os.path.relpath(fp, SRC)
            if rel in SKIP_FILES or rel.endswith(EXTRA_SKIP_SUFFIX):
                continue
            files.append((fp, rel))

    checksum_lines = []
    for fp, rel in files:
        h = hashlib.sha256(open(fp, "rb").read()).hexdigest()
        checksum_lines.append(f"{h}  {rel}")
    checksum_path = os.path.join(SRC, "CHECKSUMS.txt")
    with open(checksum_path, "w", encoding="utf-8") as f:
        f.write("# SHA256 of files in this release (verify official integrity)\n")
        f.write("\n".join(checksum_lines) + "\n")
    # 校验和随发布包一起分发，同时也保留在源码目录，便于本地核验。
    files.append((checksum_path, "CHECKSUMS.txt"))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for fp, rel in files:
            z.write(fp, os.path.join(ROOT, rel))
    print("ZIP ->", OUT, os.path.getsize(OUT) // 1024, "KB, entries:", len(files))
    print("CHECKSUMS.txt 已保留在:", checksum_path)


if __name__ == "__main__":
    main()
