@echo off
cd /d "%~dp0"
echo Launching Free Fire PRE-MATCH LOBBY calibration (2 cards, 4 boxes)...
echo Have the lobby team list on screen, scrolled to the TOP.
echo For EACH of the two top cards you draw TWO boxes:
echo   1) the TEAM NAME text only -- no logo, no 'Score: N'
echo   2) the 4 PLAYER NAMES as one tall box -- no tick marks, no MAX badges
echo Tight boxes matter: including the ticks/badges makes OCR read them as names.
echo.
echo Wrong screen? Re-run as:  calibrate_freefire_lobby.bat --monitor 1
echo.
python calibrate.py ff-lobby %*
pause
