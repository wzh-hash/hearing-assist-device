@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo  Push this project to your GitHub repository
echo ============================================
echo.

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Not a git repository.
  pause
  exit /b 1
)

if "%~1"=="" (
  set /p URL=Repo URL (https://github.com/USER/REPO.git): 
) else (
  set "URL=%~1"
)
if "!URL!"=="" (
  echo [ERROR] Empty URL.
  pause
  exit /b 1
)

echo.
echo URL: !URL!
echo.

for /f "delims=" %%i in ('git remote 2^>nul ^| findstr /x origin') do (
  git remote remove origin >nul 2>&1
)
git remote add origin "!URL!"
git branch -M main

echo [1/2] Pushing (a login window may pop up - sign in once) ...
git push -u origin main
if errorlevel 1 (
  echo.
  echo [FAILED] Push failed.
  echo   - Check repo URL and that the repo was created empty (no README).
  echo   - If login failed, run:  git push -u origin main   again.
  pause
  exit /b 1
)

echo.
echo [2/2] Done. Open: !URL!
pause
