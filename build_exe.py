"""打包 Windows exe（onedir 模式，双击即用，无需 Python）。
运行环境：隔离 venv 的 python.exe（已含 torch/open_clip/PySide6）。
产物：dist/照片视频智能分类整理/  ->  复制到发布目录。

本脚本产出「完全离线版」：把 CLIP 模型权重（约 600MB）一并打进 exe，
任何电脑断网也能直接识别，无需首次联网下载。权重在运行期由
sys._MEIPASS/models/open_clip_model.safetensors 读取（见 core/classifier.py）。

克隆到其它机器使用时：
  - 安装依赖：pip install pyinstaller torch --index-url https://download.pytorch.org/whl/cpu open_clip_torch rawpy PySide6 imageio-ffmpeg
  - 自行下载 CLIP ViT-B-32 权重（laion2B-s34B-b79K）放到本地，把下方 WEIGHTS_SRC 改成你的路径
  - 运行：python build_exe.py
"""
import os
import shutil
import subprocess
import sys

VENV = "Scripts/python.exe"  # 若用本机 venv，改成绝对路径；普通 python 也可
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(SRC_DIR, "..", "dist_release")  # 默认输出到源码仓外的发布目录
APP_NAME = "照片视频智能分类整理"

# CLIP 权重（约 600MB）。本机已下载，打进 exe 的 models/ 目录。
# 其它机器：改成你本地下载好的 open_clip_model.safetensors 绝对路径。
WEIGHTS_SRC = (
    r"C:\Users\TJM\.cache\huggingface\hub"
    r"\models--laion--CLIP-ViT-B-32-laion2B-s34B-b79K"
    r"\snapshots\1a25a446712ba5ee05982a381eed697ef9b435cf"
    r"\open_clip_model.safetensors"
)


def run(cmd):
    print(">>", " ".join(cmd) if isinstance(cmd, list) else cmd)
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("!! 步骤失败，返回码", r.returncode)
        sys.exit(r.returncode)


def main():
    os.chdir(SRC_DIR)

    if not os.path.exists(WEIGHTS_SRC):
        print("!! 未找到本机 CLIP 权重:", WEIGHTS_SRC)
        print("   请先下载权重并修改本文件顶部的 WEIGHTS_SRC 为你本机路径。")
        sys.exit(1)
    print("权重文件:", WEIGHTS_SRC, round(os.path.getsize(WEIGHTS_SRC) / 1024 / 1024), "MB")

    # 1) 装 PyInstaller（仅几 MB，不影响系统）
    run([sys.executable, "-m", "pip", "install", "pyinstaller"])

    # 2) 清理旧产物
    for d in ("build", "dist"):
        if os.path.isdir(d):
            shutil.rmtree(d)

    # 3) 打包
    #    categories.json -> 根目录
    #    权重            -> models/（运行期由 sys._MEIPASS/models/ 读取）
    add_datas = [
        ("categories.json", "."),
        (WEIGHTS_SRC, "models"),
    ]
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onedir", "--windowed",
        "--name", APP_NAME,
    ]
    for src, dst in add_datas:
        cmd += ["--add-data", src + os.pathsep + dst]
    cmd += [
        "--hidden-import", "open_clip",
        "--hidden-import", "rawpy",
        "--hidden-import", "safetensors",
        "--hidden-import", "PIL",
        "--hidden-import", "numpy",
        "app.py",
    ]
    run(cmd)

    # 4) 把 exe 文件夹复制到发布目录
    os.makedirs(OUT_DIR, exist_ok=True)
    src = os.path.join(SRC_DIR, "dist", APP_NAME)
    if not os.path.isdir(src):
        print("!! 未找到打包产物:", src)
        sys.exit(1)
    dst = os.path.join(OUT_DIR, APP_NAME + "_windows_offline")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    print("EXE 已复制到:", dst)

    # 5) 打包成 zip 方便分发
    zip_path = os.path.join(OUT_DIR, APP_NAME + "_windows_v1.0.1_offline.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    shutil.make_archive(os.path.splitext(zip_path)[0], "zip", OUT_DIR, APP_NAME + "_windows_offline")
    print("EXE 发布包:", zip_path)
    print("BUILD_ALL_DONE")


if __name__ == "__main__":
    main()
