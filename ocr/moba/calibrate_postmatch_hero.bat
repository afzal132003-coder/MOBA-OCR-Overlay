@echo off
cd /d "%~dp0"
echo Recalibrating ONLY the post-game HERO DAMAGE / DAMAGE TAKEN boxes (Data screen, 20 boxes)...
echo Have that screen up on your monitor before continuing.
pause
python calibrate_postmatch_hero.py
pause
