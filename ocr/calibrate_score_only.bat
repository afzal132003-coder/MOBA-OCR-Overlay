@echo off
cd /d "%~dp0"
echo Recalibrating ONLY the series score boxes (team1_series_score, team2_series_score)...
python calibrate_hud.py team1_series_score team2_series_score
pause
