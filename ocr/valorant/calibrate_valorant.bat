@echo off
cd /d "%~dp0"
echo Launching FULL Valorant calibration (in-game + character-select + post-match + live player stats, all four)...
python calibrate_valorant.py
pause
