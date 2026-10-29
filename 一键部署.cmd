@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"
set TITLE=LawAgent 本地版 · 一键部署
title %TITLE%

rem ---------- 0. 版本检查：换版本自动清理旧运行文件 ----------
set VERSION=
if exist VERSION set /p VERSION=<VERSION
set INSTALLED=
if exist "data\.installed_version" set /p INSTALLED=<data\.installed_version
if not "%INSTALLED%"=="" if not "%VERSION%"=="%INSTALLED%" (
  echo [版本变化] 旧环境 %INSTALLED%  ^-^>  新版本 %VERSION%
  echo            正在清理旧虚拟环境与本地数据，避免版本串味...
  if exist ".venv" rmdir /s /q ".venv"
  del /q "data\settings.json" 2>nul
  del /q "data\db\docs.db" 2>nul
  if exist "data\docs" rmdir /s /q "data\docs"
  if exist "data\users" rmdir /s /q "data\users"
  echo            清理完成。
)

rem 停掉可能残留的旧实例（占用 8787）
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8787" ^| findstr LISTENING') do taskkill /PID %%p /F >nul 2>&1


echo ==========================================
echo   LawAgent 本地版（单用户 / 免登录 / 不联网发布）
echo ==========================================
echo.

rem ---------- 1. Python ----------
set PY=
where py >nul 2>&1 && set PY=py -3
if not defined PY (
  where python >nul 2>&1 && set PY=python
)
if not defined PY (
  echo [错误] 没找到 Python。
  echo 请先安装 Python 3.11 或更新版本，安装时勾选 "Add python.exe to PATH"。
  echo 下载： https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

rem ---------- 2. venv + deps ----------
if not exist ".venv\Scripts\python.exe" (
  echo [1/4] 创建虚拟环境 .venv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [错误] 虚拟环境创建失败。
    pause
    exit /b 1
  )
  echo [2/4] 安装依赖 ...
  if exist "vendor\wheels" (
    ".venv\Scripts\python.exe" -m pip install -q --no-index --find-links vendor\wheels setuptools wheel packaging
    ".venv\Scripts\python.exe" -m pip install -q --no-index --find-links vendor\wheels jieba==0.42.1
  ) else (
    ".venv\Scripts\python.exe" -m pip install -q jieba==0.42.1 -i https://pypi.tuna.tsinghua.edu.cn/simple
  )
  if errorlevel 1 (
    echo [错误] 依赖安装失败。联网安装命令：
    echo   .venv\Scripts\python.exe -m pip install jieba==0.42.1 -i https://pypi.tuna.tsinghua.edu.cn/simple
    pause
    exit /b 1
  )
) else (
  echo [1/4] 已检测到 .venv，跳过创建。
  echo [2/4] 跳过依赖安装。
)

rem ---------- 3. 数据自检 ----------
if not exist "data\db\articles.db" (
  echo [错误] 缺少法条库 data\db\articles.db，请确认压缩包完整解压。
  pause
  exit /b 1
)
echo [3/4] 法条库检查通过。

rem ---------- 4. Ollama ----------
echo [4/4] 检查 Ollama ...
netstat -ano | findstr ":11434" | findstr LISTENING >nul
if errorlevel 1 (
  if exist "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe" (
    start "" /min "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
    ping -n 7 127.0.0.1 >nul
  ) else (
    echo [提示] 没检测到 Ollama。请先安装：https://ollama.com/download
    echo        并执行： ollama pull qwen3.5:9b ^&^& ollama pull bge-m3
  )
)

echo.
echo 启动服务： http://127.0.0.1:8787
start "" ".venv\Scripts\python.exe" serve\app.py
ping -n 5 127.0.0.1 >nul
start "" http://127.0.0.1:8787

echo.
echo 已启动。首次使用提示：
echo   - 默认模型 qwen3.5:9b；若本机没有，改 configs\config.json 的 llm.model
echo   - 没有 bge-m3 时自动退回纯全文检索
echo   - 本版本没有登录、没有管理员后台，只监听 127.0.0.1
> "data\.installed_version" echo %VERSION%
echo.
pause
