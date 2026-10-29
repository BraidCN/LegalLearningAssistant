@echo off
chcp 65001 >nul
cd /d %~dp0
echo [1/3] 拉取 just-laws 数据 ...
.venv\Scripts\python.exe build\fetch.py
if errorlevel 1 goto :err
echo [2/3] 解析条级文本 ...
.venv\Scripts\python.exe build\parse_articles.py
if errorlevel 1 goto :err
echo [3/3] 建立索引库 ...
.venv\Scripts\python.exe build\build_db.py
if errorlevel 1 goto :err
echo.
echo 完成。双击“启动.cmd”即可使用。
pause
exit /b 0

:err
echo.
echo 构建出错，请查看上面的输出。
pause
exit /b 1
