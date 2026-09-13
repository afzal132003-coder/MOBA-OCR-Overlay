@echo off
setlocal
cd /d "%~dp0"

echo ==========================================================
echo   Updating MOBA-OCR-Overlay to the latest code
echo ==========================================================
echo.
echo Folder: %CD%
for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set BRANCH=%%b
if "%BRANCH%"=="" (
  echo.
  echo This folder is not a git checkout, so there is nothing to pull.
  echo Make sure you are running this from inside the MOBA-OCR-Overlay folder.
  echo.
  pause
  exit /b 1
)
echo Branch: %BRANCH%
echo.

REM --autostash is the point of this script. The engine rewrites its own
REM config and state files as it runs, so there are almost always local
REM changes sitting in the checkout. Without it git refuses to pull and
REM prints advice that reads like something is broken. With it, those
REM changes are set aside, the new code comes down, and they are put back.
echo Fetching and merging...
echo.
git pull --rebase --autostash
if errorlevel 1 goto :failed

echo.
echo ==========================================================
echo   Updated. You are now on:
echo ==========================================================
git --no-pager log -1 --format="  %%h  %%s"
echo.
echo If the Free Fire engine is running, close it and start it again --
echo it only reads the new code when it starts.
echo.
pause
exit /b 0

:failed
echo.
echo ==========================================================
echo   The update did not finish.
echo ==========================================================
echo.
echo Nothing has been lost. The usual causes:
echo.
echo   * No internet, or GitHub is unreachable.
echo   * The same file was edited here AND in the new code, so git
echo     cannot decide which version wins.
echo.
echo Read the message above -- it names the file. Then either send it
echo to Claude, or undo your own edit to that one file with:
echo.
echo     git checkout -- ^<the file it named^>
echo.
echo and run this again.
echo.
pause
exit /b 1
