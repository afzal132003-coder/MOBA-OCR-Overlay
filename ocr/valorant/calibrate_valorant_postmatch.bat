@echo off
cd /d "%~dp0"
echo Launching Valorant POST-MATCH calibration (10x5 stat grid: K/D/A, ACS, K/D, First Bloods, Plants)...
python calibrate_valorant.py valo-postmatch
pause
