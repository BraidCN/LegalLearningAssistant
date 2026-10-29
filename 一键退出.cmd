@echo off
chcp 936 >nul
cd /d "%~dp0"
title LawAgent 本地版 · 一键退出
echo 正在关闭看门狗与问答服务 ...
if exist "tools\stop-local.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "tools\stop-local.ps1"
  ping -n 3 127.0.0.1 >nul
  powershell -NoProfile -ExecutionPolicy Bypass -File "tools\stop-local.ps1"
) else (
  for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8787" ^| findstr LISTENING') do taskkill /PID %%p /F >nul 2>&1
)
echo.
echo 已关闭。Ollama 仍在运行（如需关闭：任务栏退出 Ollama）。
echo 重新启动：双击 启动.cmd
pause
