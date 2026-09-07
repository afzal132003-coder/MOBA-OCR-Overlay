@echo off
cd /d "%~dp0"
echo Launching Dota 2 POSTGAME - NET WORTH AND K/D/A calibration...
echo Have the post-match OVERVIEW screen up (K/D/A, Net Worth, team score, duration all visible).
python calibrate.py postgame-net-kda %*
pause
