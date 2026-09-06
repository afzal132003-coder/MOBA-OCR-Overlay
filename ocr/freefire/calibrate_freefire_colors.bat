@echo off
cd /d "%~dp0"
echo Launching Free Fire SIDE-TABLE BAR COLOUR calibration...
echo Have a match running with a MIX of squad states visible on the side table:
echo   some squads full, at least one knocked, at least one wiped.
echo You will click one example bar of each state.
echo.
python calibrate_sidetable_colors.py %*
pause
