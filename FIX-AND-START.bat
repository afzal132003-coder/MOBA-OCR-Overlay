@echo off
REM ---------------------------------------------------------------------
REM  Close the engine window first, then double-click this.
REM  It just restarts the engine. The engine only reads the code when it
REM  starts, so nothing that was changed takes effect until you do this.
REM
REM  The log reader stays ON: you keep the kill feed, automatic match-end
REM  result fetching, and match start/end detection.
REM
REM  What the alive side table does now:
REM
REM   * It reads the tags and the kill numbers by matching the client's
REM     own font, not by OCR. Roughly a hundred times quicker, so every
REM     row is read on every poll instead of a few of them.
REM   * It watches for the client's own "#7 TEAM ELIMINATED" banner, so a
REM     wipe goes up the moment the game says so and brings the finishing
REM     place with it. Needs ff-elim-banner calibrated; without it this
REM     does nothing and the alive bars carry the wipe as before.
REM   * A row whose name it could not read this poll keeps the squad it
REM     last read there, instead of falling back to the dropdown -- a
REM     stale dropdown is what put one squad's kills under another name.
REM   * The one connection out to the cloud relay is written to less
REM     often than the pages on this machine. That link is the reason the
REM     graphic trailed the game while the dashboard looked instant.
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
