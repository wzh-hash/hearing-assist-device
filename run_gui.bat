@echo off
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set "BUNDLED=C:\Users\MR\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if exist "%BUNDLED%" goto usebundled
goto usepath
:usebundled
"%BUNDLED%" -X utf8 pc_gui.py %*
goto end
:usepath
python -X utf8 pc_gui.py %*
:end
if errorlevel 1 pause
