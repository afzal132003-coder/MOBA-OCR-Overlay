@echo off
cd /d "%~dp0"
echo Recalibrating ONLY the post-game GOLD boxes (Overall screen, 10 boxes)...
echo Have that screen up on your monitor before continuing.
pause
python calibrate_postmatch_gold.py
pause
