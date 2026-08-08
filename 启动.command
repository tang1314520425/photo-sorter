#!/bin/bash
# 照片视频智能分类整理 - macOS 启动脚本
DIR="$(cd "$(dirname "$0")" && pwd)"
export HF_ENDPOINT=https://hf-mirror.com
export PYTHONUTF8=1
PYEXE=""
[ -x "$DIR/venv/bin/python3" ] && PYEXE="$DIR/venv/bin/python3"
[ -z "$PYEXE" ] && PYEXE="$(command -v python3 || command -v python)"
if [ -z "$PYEXE" ]; then
  echo "未找到 Python，请先安装 Python 3.10+ 或运行 安装依赖.command"
  exit 1
fi
"$PYEXE" "$DIR/app.py"
