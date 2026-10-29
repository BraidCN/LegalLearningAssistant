@echo off
cd /d "%~dp0"
echo [1/2] Pulling latest lawtext and applying changes ...
.venv\Scripts\python.exe build\update_lawtext.py
if errorlevel 1 goto :rebuild
echo No changes. Nothing to rebuild.
goto :done

:rebuild
echo [2/2] Changes found, rebuilding vector index (about 13 min) ...
.venv\Scripts\python.exe build\embed.py --no-resume
echo Done.

:done
pause
