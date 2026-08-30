@echo off
cd /d "%~dp0"
echo Recalibrating ONLY the post-game K/D/A boxes (10 boxes, one per player)...
echo Have that screen up on your monitor before continuing.
pause
python calibrate_postmatch_kda.py
pause
