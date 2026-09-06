@echo off
cd /d "%~dp0"
echo Launching Free Fire SIDE-TABLE COLUMN calibration...
echo Have a match running with the 12-team side table visible.
echo You will drag three boxes: TEAM NAME (not the logo), ELIMS, and the ALIVE bars.
echo.
echo Wrong screen? Re-run as:  calibrate_freefire_columns.bat --monitor 1
echo.
python calibrate_sidetable_columns.py %*
pause
