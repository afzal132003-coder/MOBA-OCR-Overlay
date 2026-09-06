@echo off
cd /d "%~dp0"
echo Launching Free Fire PRE-MATCH LOBBY calibration (4 squad blocks)...
echo Have the lobby team list on screen, scrolled to the TOP.
echo Draw around each squad's whole block: team name AND the player names under it.
echo.
echo Wrong screen? Re-run as:  calibrate_freefire_lobby.bat --monitor 1
echo.
python calibrate.py ff-lobby %*
pause
