#!/bin/bash
# 照片视频智能分类整理 - macOS 环境安装脚本
DIR="$(cd "$(dirname "$0")" && pwd)"
export HF_ENDPOINT=https://hf-mirror.com
MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple
PYEXE=""
[ -x "$DIR/venv/bin/python3" ] && PYEXE="$DIR/venv/bin/python3"
[ -z "$PYEXE" ] && PYEXE="$(command -v python3 || command -v python)"
if [ -z "$PYEXE" ]; then
  echo "未找到 Python，请先安装 Python 3.10+"
  exit 1
fi
echo "安装基础组件..."
"$PYEXE" -m pip install -i $MIRROR pillow numpy imageio-ffmpeg
echo "是否安装语义识别组件（约600MB）？[y/N]"
read ans
if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
  "$PYEXE" -m pip install torch open_clip_torch rawpy
  "$PYEXE" -c "import os;os.environ['HF_ENDPOINT']='https://hf-mirror.com';import open_clip;open_clip.create_model_and_transforms('ViT-B-32',pretrained='laion2b_s34b_b79k');print('模型就绪')"
fi
echo "安装界面组件..."
"$PYEXE" -m pip install -i $MIRROR PySide6-Essentials
echo "装好了。运行 启动.command 即可使用"
