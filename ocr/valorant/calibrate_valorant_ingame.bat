@echo off
cd /d "%~dp0"
echo Launching Valorant IN-GAME calibration (round scores + spike/defuse banner)...
python calibrate_valorant.py valo-ingame
pause
