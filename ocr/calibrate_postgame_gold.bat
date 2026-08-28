@echo off
cd /d "%~dp0"
echo Recalibrating ONLY the post-game GOLD boxes (Overall screen, 10 boxes)...
echo Have that screen up on your monitor before continuing.
pause
python calibrate_postmatch.py postgame_gold_team1_0 postgame_gold_team1_1 postgame_gold_team1_2 postgame_gold_team1_3 postgame_gold_team1_4 postgame_gold_team2_0 postgame_gold_team2_1 postgame_gold_team2_2 postgame_gold_team2_3 postgame_gold_team2_4
pause
