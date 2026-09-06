@echo off
cd /d "%~dp0"
echo Launching Free Fire LOADOUT calibration (8 boxes: whole card, IGN, active, 3 passives, pet, equipment)...
echo Have a player's loadout card on screen -- the same one Num5 captures.
echo.
echo Wrong screen? Re-run as:  calibrate_freefire_loadout.bat --monitor 1
echo.
python calibrate.py ff-loadout %*
pause
