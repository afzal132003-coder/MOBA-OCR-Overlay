@echo off
cd /d "%~dp0"
echo BGMI calibration -- one region, the Android picture.
echo.
echo Have the observer TEAM PANEL open in the game first.
echo.
echo Drag the box around the GAME PICTURE ONLY. Leave out the BlueStacks
echo title bar at the top and the toolbar down the right -- if either is
echo included, every offset shifts by its size and nothing reads.
echo.
echo It reads the panel back afterwards and tells you what it found, so
echo you will know straight away whether the box is right.
echo.
echo Wrong screen? Run:  calibrate_bgmi.bat --monitor 1
echo.
python calibrate_bgmi.py %*
pause
