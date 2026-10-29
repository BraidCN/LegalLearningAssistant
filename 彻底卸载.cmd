@echo off
chcp 936 >nul
cd /d "%~dp0"
title LawAgent · 彻底卸载
echo [警告] 将停止看门狗与服务，并删除整个目录（含法条库），不可恢复。
set /p SURE=确认请输入 YES 回车：
if /i not "%SURE%"=="YES" exit /b 0
if exist "tools\stop-local.ps1" powershell -NoProfile -ExecutionPolicy Bypass -File "tools\stop-local.ps1"
ping -n 2 127.0.0.1 >nul
set TARGET=%~dp0
if "%TARGET:~-1%"=="\" set TARGET=%TARGET:~0,-1%
echo 正在删除 %TARGET% ...
cd /d "%TEMP%"
powershell -NoProfile -Command "Start-Sleep -Seconds 2; Remove-Item -LiteralPath '%TARGET%' -Recurse -Force -ErrorAction SilentlyContinue"
exit /b 0
