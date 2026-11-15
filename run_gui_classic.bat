@echo off
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if exist "runtime\python.exe" goto useruntime

set "BUNDLED=C:\Users\MR\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if exist "%BUNDLED%" goto usebundled
goto usepath

:useruntime
"runtime\python.exe" -X utf8 src\pc_gui.py %*
goto end

:usebundled
"%BUNDLED%" -X utf8 src\pc_gui.py %*
goto end

:usepath
python -X utf8 src\pc_gui.py %*

:end
if errorlevel 1 pause
