@echo off
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

set "BUNDLED=C:\Users\MR\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"

if exist "%BUNDLED%" goto usebundled
goto usepath

:usebundled
"%BUNDLED%" -X utf8 src\pc_recognizer.py %*
goto after

:usepath
python -X utf8 src\pc_recognizer.py %*
goto after

:after
if "%~1"=="" (
  echo.
  echo Usage:
  echo   run.bat devices
  echo   run.bat record-ref --takes 3
  echo   run.bat calibrate
  echo   run.bat listen
  echo   run.bat listen --quiet --log recog.log
  echo.
  pause
)
