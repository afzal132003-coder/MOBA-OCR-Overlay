@echo off
cd /d "%~dp0"
echo Launching Valorant LIVE PLAYER STATS calibration (40 cells: 2 teams x 5 rows x Kills/Deaths/Assists/Coins)...
echo Have the in-game Tab-held scoreboard on screen (hold Tab, or use tools\valorant_tab_hold.ahk).
python calibrate_valorant.py valo-livestats
pause
