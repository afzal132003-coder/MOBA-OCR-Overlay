@echo off
REM ---------------------------------------------------------------------
REM  Close the engine window first, then double-click this.
REM  It just restarts the engine with today's fixes.
REM
REM  The log reader stays ON: you keep the kill feed, automatic match-end
REM  result fetching, and match start/end detection. What changed is that
REM  the alive GRID now owns the side table while it is reading, so the
REM  log's fresh-lobby picture can no longer overwrite it -- that fight is
REM  what made squads die and come back, and what stalled the engine so
REM  your ticks never arrived.
REM ---------------------------------------------------------------------

cd /d "%~dp0"

echo.
echo  [1/3] Checking the engine is closed...
tasklist /FI "IMAGENAME eq python3.12.exe" 2>nul | find /I "python3.12.exe" >nul
if not errorlevel 1 (
    echo.
    echo  *** The engine is STILL RUNNING. ***
    echo  Close its black window first, then double-click this again.
    echo.
    pause
    exit /b 1
)
echo        ok, nothing running.

echo  [2/3] Making sure the log reader is ON (kill feed, auto results)...
python tools\set-debugger-folder.py
if errorlevel 1 (
    echo.
    echo  Could not check the setting -- stopping so nothing is half-done.
    pause
    exit /b 1
)

echo  [3/3] Starting the engine...
echo.
call "%~dp0ocr\start_freefire.bat"
