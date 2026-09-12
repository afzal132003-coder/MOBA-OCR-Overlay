@echo off
cd /d "%~dp0"
echo Launching Free Fire ALIVE GRID calibration (4 anchor boxes)...
echo Have a match actually RUNNING, with the 12-team side table visible.
echo.
echo Draw a TIGHT box around exactly one alive indicator / number each time --
echo   1. Row 1 (topmost team), Player 1 (leftmost) alive indicator
echo   2. Row 1, Player 2 (one slot right of box 1)
echo   3. LAST row (bottom-most team, row 12), Player 1 -- NOT row 2
echo   4. Row 1's elimination count number
echo.
echo Uses the screen set in freefire_config.json ("monitor"). If the screenshot
echo that opens is the WRONG screen, run this instead:
echo     calibrate_freefire_alive_grid.bat --monitor 1
echo.
python calibrate.py ff-alive-grid %*
pause
