@echo off
cd /d "%~dp0"
echo Launching Free Fire LIVE IN-GAME calibration (killfeed + 12-team side table)...
echo Have a match actually RUNNING, with the killfeed and side table visible.
echo.
echo Uses the screen set in freefire_config.json ("monitor"). If the screenshot
echo that opens is the WRONG screen, run this instead:
echo     calibrate_freefire_live.bat --monitor 1
echo.
python calibrate.py ff-live %*
pause
