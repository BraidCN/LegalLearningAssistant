@echo off
chcp 936 >nul
cd /d "%~dp0"
title LawAgent 本地版
if not exist ".venv\Scripts\python.exe" (
  echo [提示] 还没有部署运行环境。
  echo        请先双击「一键部署.cmd」，完成后再用这个脚本。
  pause
  exit /b 1
)
if not exist "data\db\articles.db" (
  echo [错误] 缺少法条库 data\db\articles.db，见 data\README.md 的说明。
  pause
  exit /b 1
)
start "LawAgent" ".venv\Scripts\python.exe" serve\app.py
ping -n 4 127.0.0.1 >nul
start "" http://127.0.0.1:8787
