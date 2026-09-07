@echo off
cd /d "%~dp0"
echo Launching Dota 2 POST-MATCH calibration (13 boxes: 10 players, 2 team scores, duration)...
echo Have the post-match Overview screen up (kills/deaths/assists, gold, team score, duration all visible).
python calibrate.py %*
pause
