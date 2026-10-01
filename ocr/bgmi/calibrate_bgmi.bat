@echo off
cd /d "%~dp0"
echo BGMI calibration -- one region, the Android picture.
echo.
echo Have the observer TEAM PANEL open in the game first.
echo.
echo It LISTS YOUR SCREENS and asks which one the game is on, so you do
echo not have to know the number. This rig runs the game on screen 2.
echo.
echo Then drag the box around the GAME PICTURE ONLY. Leave out the
echo BlueStacks title bar at the top and the toolbar down the right -- if
echo either is included, every offset shifts by its size and nothing reads.
echo.
echo It reads the panel back afterwards and tells you what it found, so
echo you know straight away whether the box is right.
echo.
echo To skip the question:  calibrate_bgmi.bat --monitor 2
echo.
python calibrate_bgmi.py %*
pause
