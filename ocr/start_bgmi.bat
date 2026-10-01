@echo off
cd /d "%~dp0bgmi"
echo Starting BGMI panel engine on port 8767...
echo.
echo Its own process and its own port, so this does NOT clash with the
echo Free Fire engine (8765) or Dota 2 (8766) -- they can run side by side.
echo.
echo Open the dashboard, pick the BGMI tab, and have the observer team
echo panel up in the game before capturing a slide.
echo.
python bgmi_engine.py --serve
pause
