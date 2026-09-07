@echo off
cd /d "%~dp0"
echo Launching Dota 2 DAMAGE CALIB POSTGAME calibration...
echo Have the post-match DAMAGE tab up (Scoreboard/Breakdowns -- whichever screen shows it).
python calibrate.py postgame-damage %*
pause
