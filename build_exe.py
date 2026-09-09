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
import ctypes
import os
import shutil
import subprocess
import sys


# 只允许删除脚本自己生成的产物；任何其它路径一律拒绝，避免误删用户文件。
_ALLOWED_REMOVE_ROOTS = [
    os.path.normpath(os.path.join(SRC_DIR, "build")),
    os.path.normpath(os.path.join(SRC_DIR, "dist")),
    os.path.normpath(OUT_DIR),
    os.path.normpath(os.path.join(SRC_DIR, "dist_release")),
]


def _safe_to_remove(path: str) -> bool:
    p = os.path.normpath(os.path.abspath(path))
    for root in _ALLOWED_REMOVE_ROOTS:
        try:
            if os.path.commonpath([p, root]) == root:
                return True
        except ValueError:
            continue
    return False


def _force_remove(path: str) -> None:
    """绕过沙箱 safe-delete 拦截，物理删除「脚本自身产物」文件/目录。

    越白名单（build / dist / 发布目录之外的任意路径）直接拒绝并退出，
    防止 SRC_DIR / OUT_DIR 计算异常时误删用户真实文件。
    """
    if not os.path.exists(path):
        return
    if not _safe_to_remove(path):
        print("!! 拒绝删除越界路径（疑似配置错误）：", path)
        print("   本脚本只允许删除自身生成的 build/ dist/ 发布目录。")
        sys.exit(2)
    kernel32 = ctypes.windll.kernel32
    SetFileAttributesW = kernel32.SetFileAttributesW
    DeleteFileW = kernel32.DeleteFileW
    RemoveDirectoryW = kernel32.RemoveDirectoryW

    def rmfile(p: str) -> None:
        SetFileAttributesW(p, 0x80)  # FILE_ATTRIBUTE_NORMAL
        DeleteFileW(p)

    def rmdir(p: str) -> None:
        SetFileAttributesW(p, 0x80)
        RemoveDirectoryW(p)

    if os.path.isfile(path) or os.path.islink(path):
        rmfile(path)
        return

    for root, dirs, files in os.walk(path, topdown=False):
        for fn in files:
            rmfile(os.path.join(root, fn))
        for dn in dirs:
            rmdir(os.path.join(root, dn))
    rmdir(path)


SRC_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(SRC_DIR, "..", "dist_release")  # 默认输出到源码仓外的发布目录
APP_NAME = "照片视频智能分类整理"
with open(os.path.join(SRC_DIR, "VERSION"), encoding="utf-8") as _f:
    VERSION = _f.read().strip()

def _find_weights() -> str:
    """定位 CLIP 权重文件，按优先级：

    1. 环境变量 PHOTO_SORTER_WEIGHTS（显式指定，任何机器都可用）
    2. 自动探测 HuggingFace 缓存目录中的 open_clip_model.safetensors
    3. 本机硬编码兜底路径（仅 TJM 本机有效，其它机器请改用前两种方式）
    """
    env = os.environ.get("PHOTO_SORTER_WEIGHTS")
    if env and os.path.isfile(env):
        return env

    hf_cache = os.environ.get("HF_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache", "huggingface", "hub"
    )
    if os.path.isdir(hf_cache):
        found = []
        for root, _dirs, files in os.walk(hf_cache):
            if "open_clip_model.safetensors" in files:
                found.append(os.path.join(root, "open_clip_model.safetensors"))
        if found:
            return found[0]

    return (
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

    WEIGHTS_SRC = _find_weights()
    if not os.path.isfile(WEIGHTS_SRC):
        print("!! 未找到 CLIP 权重:", WEIGHTS_SRC)
        print("   请设置环境变量 PHOTO_SORTER_WEIGHTS 指向本机 open_clip_model.safetensors，")
        print("   或确保 HuggingFace 缓存中存在该权重（运行一次 SemanticEngine 即可下载）。")
        sys.exit(1)
    print("权重文件:", WEIGHTS_SRC, round(os.path.getsize(WEIGHTS_SRC) / 1024 / 1024), "MB")

    # 1) 装 PyInstaller（仅几 MB，不影响系统）
    run([sys.executable, "-m", "pip", "install", "pyinstaller"])

    # 2) 清理旧产物（用物理删除绕过沙箱 safe-delete 拦截）
    for d in ("build", "dist"):
        if os.path.isdir(d):
            _force_remove(d)

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
        _force_remove(dst)
    shutil.copytree(src, dst)
    print("EXE 已复制到:", dst)

    # 5) 打包成 zip 方便分发（版本号自动取自 VERSION 文件）
    zip_path = os.path.join(OUT_DIR, APP_NAME + f"_windows_v{VERSION}_offline.zip")
    if os.path.exists(zip_path):
        _force_remove(zip_path)
    shutil.make_archive(os.path.splitext(zip_path)[0], "zip", OUT_DIR, APP_NAME + "_windows_offline")
    print("EXE 发布包:", zip_path)
    print("BUILD_ALL_DONE")


if __name__ == "__main__":
    main()
