@echo off
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
