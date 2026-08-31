@echo off
cd /d "%~dp0"
echo Launching Valorant CHARACTER-SELECT calibration (10 agent portrait slots)...
python calibrate_valorant.py valo-character
pause
