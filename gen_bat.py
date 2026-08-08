import os

base = r'C:\Users\TJM\WorkBuddy\2026-08-08-11-19-20\photo_sorter'

launch = r'''@echo off
chcp 65001 >nul
title 照片视频智能分类整理

set "HF_ENDPOINT=https://hf-mirror.com"
set "PYTHONUTF8=1"
set "PYEXE="

:: 1) 便携 venv（与脚本同目录的 venv）
if exist "%~dp0venv\Scripts\pythonw.exe" set "PYEXE=%~dp0venv\Scripts\pythonw.exe"
:: 2) 本机 WorkBuddy 专用 venv（仅 TJM 这台机器有效，保留作回退，不影响其他用户）
if not defined PYEXE if exist "C:\Users\TJM\.workbuddy\binaries\python\envs\photosort\Scripts\pythonw.exe" set "PYEXE=C:\Users\TJM\.workbuddy\binaries\python\envs\photosort\Scripts\pythonw.exe"
:: 3) 系统 Python
if not defined PYEXE (
  where pythonw >nul 2>&1 && set "PYEXE=pythonw"
)
if not defined PYEXE (
  where python >nul 2>&1 && set "PYEXE=python"
)

if not defined PYEXE (
  echo.
  echo   找不到 Python 运行环境。
  echo   请先运行「安装依赖.bat」或安装 Python 3.10+。
  echo.
  pause
  exit /b 1
)

start "" "%PYEXE%" "%~dp0app.py"
exit /b 0
'''

install = r'''@echo off
chcp 65001 >nul
title 安装运行环境

set "MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"
set "HF_ENDPOINT=https://hf-mirror.com"
set "PYEXE="

:: 1) 便携 venv
if exist "%~dp0venv\Scripts\python.exe" set "PYEXE=%~dp0venv\Scripts\python.exe"
:: 2) 本机 WorkBuddy venv（仅 TJM 机器，保留回退）
if not defined PYEXE if exist "C:\Users\TJM\.workbuddy\binaries\python\envs\photosort\Scripts\python.exe" set "PYEXE=C:\Users\TJM\.workbuddy\binaries\python\envs\photosort\Scripts\python.exe"
:: 3) 系统 Python
if not defined PYEXE (
  for /f "delims=" %%i in ('where py 2^>nul') do ( set "PYEXE=%%i" & goto :havepy )
)
:havepy
if not defined PYEXE (
  for /f "delims=" %%i in ('where python 2^>nul') do ( set "PYEXE=%%i" & goto :havepy2 )
)
:havepy2
if not defined PYEXE (
  echo 找不到 Python，请先安装 Python 3.10+ 后重试。
  pause & exit /b 1
)

echo.
echo ============================================================
echo   照片视频智能分类整理 - 环境安装
echo   全部装在独立环境里，不会影响你电脑上其它 Python
echo ============================================================
echo.

if "%PYEXE%"=="%~dp0venv\Scripts\python.exe" (
  echo [1/3] 便携环境已存在，跳过。
) else (
  echo [1/3] 使用现有 Python：%PYEXE%
)

echo.
echo [2/3] 安装基础组件（约 60MB）...
"%PYEXE%" -m pip install -i %MIRROR% pillow numpy imageio-ffmpeg
if errorlevel 1 goto :fail

echo.
echo [3/3] 安装语义识别组件（CPU 版，约 600MB，可跳过但强烈建议装）...
echo       不装也能用，但只能按「动图/截图」这类规则整理。
choice /c YN /n /m "现在安装语义识别？(Y=装 / N=跳过) "
if errorlevel 2 goto :qt
"%PYEXE%" -m pip install --index-url https://download.pytorch.org/whl/cpu torch
if errorlevel 1 goto :fail
"%PYEXE%" -m pip install -i %MIRROR% open_clip_torch rawpy
if errorlevel 1 goto :fail

echo.
echo 正在预下载视觉模型（约 600MB，只需一次，之后永久离线可用）...
"%PYEXE%" -c "import os;os.environ['HF_ENDPOINT']='https://hf-mirror.com';import open_clip;open_clip.create_model_and_transforms('ViT-B-32',pretrained='laion2b_s34b_b79k');print('模型就绪')"

:qt
echo.
echo 安装界面组件...
"%PYEXE%" -m pip install -i %MIRROR% PySide6-Essentials
if errorlevel 1 goto :fail

:done
echo.
echo ============================================================
echo   装好了。双击「启动.bat」即可使用。
echo ============================================================
pause
exit /b 0

:fail
echo.
echo   安装出错，请把上面的红字截图发出来。
pause
exit /b 1
'''

for fn, txt in [('启动.bat', launch), ('安装依赖.bat', install)]:
    with open(os.path.join(base, fn), 'w', encoding='gbk') as f:
        f.write(txt)
    print('wrote', fn, os.path.getsize(os.path.join(base, fn)), 'bytes (gbk)')
