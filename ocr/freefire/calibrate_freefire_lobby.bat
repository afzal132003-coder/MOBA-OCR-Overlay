@echo off
cd /d "%~dp0"
echo Launching Free Fire PRE-MATCH LOBBY calibration (2 squad cards)...
echo Have the lobby team list on screen, scrolled to the TOP.
echo Draw around each squad's whole card: team name AND the player names under it.
echo Use the two fully-visible top cards -- not ones clipped by the spectator bar.
echo.
echo Wrong screen? Re-run as:  calibrate_freefire_lobby.bat --monitor 1
echo.
python calibrate.py ff-lobby %*
pause
