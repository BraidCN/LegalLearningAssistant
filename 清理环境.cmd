@echo off
chcp 936 >nul
cd /d "%~dp0"
title LawAgent · 清理运行环境（保留法条库）
echo 将停止看门狗与服务，并删除 .venv、缓存、本地设置、用户资料、日志（法条库保留）
set /p OK=继续请输入 y 回车：
if /i not "%OK%"=="y" exit /b 0
if exist "tools\stop-local.ps1" powershell -NoProfile -ExecutionPolicy Bypass -File "tools\stop-local.ps1"
ping -n 2 127.0.0.1 >nul
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8787" ^| findstr LISTENING') do taskkill /PID %%p /F >nul 2>&1
if exist ".venv" rmdir /s /q ".venv"
for /d /r %%d in (__pycache__) do if exist "%%d" rmdir /s /q "%%d"
if exist "data\settings.json" del /q "data\settings.json"
if exist "data\.installed_version" del /q "data\.installed_version"
if exist "data\db\docs.db" del /q "data\db\docs.db"
if exist "data\docs" rmdir /s /q "data\docs"
if exist "data\users" rmdir /s /q "data\users"
if exist "tools\watchdog.log" del /q "tools\watchdog.log"
if exist "deploy\watchdog.lock" del /q "deploy\watchdog.lock"
del /q "*.log" 2>nul
echo.
echo 清理完成，法条库已保留：data\db\articles.db
echo 下一步：解压新版本 → 双击「一键部署.cmd」。
pause
