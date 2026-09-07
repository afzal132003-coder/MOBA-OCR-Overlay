@echo off
cd /d "%~dp0"
echo Starting Dota 2 post-match server...
echo Open dota2_dashboard.html in a browser once this says "listening".
python dota2_engine.py --serve
pause
