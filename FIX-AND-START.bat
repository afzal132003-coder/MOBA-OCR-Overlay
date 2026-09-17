@echo off
REM ---------------------------------------------------------------------
REM  Use this INSTEAD of ocr\start_freefire.bat, once.
REM  Close the engine window first, then double-click this.
REM
REM  It turns off the engine's debugger-log reader, then starts the engine
REM  exactly the way start_freefire.bat does.
REM
REM  Why: the log reader skipped the log's history on purpose (so it would
REM  not fire seventy old eliminations onto air), which leaves it thinking
REM  the match is a fresh lobby -- everyone alive, nobody with kills. It
REM  publishes that picture whenever the alive grid misses a poll, so the
REM  table flips between two truths, teams appear to die and come back,
REM  and the engine stalls long enough that your ticks never arrive.
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

echo  [2/3] Turning off the log reader...
python tools\set-debugger-folder.py "C:\Temp\nologs"
if errorlevel 1 (
    echo.
    echo  Could not change the setting -- stopping, so nothing is half-done.
    pause
    exit /b 1
)

echo  [3/3] Starting the engine...
echo.
call "%~dp0ocr\start_freefire.bat"
