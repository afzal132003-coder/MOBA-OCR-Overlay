@echo off
cd /d "%~dp0"
echo Recalibrating ONLY the post-game HERO DAMAGE / DAMAGE TAKEN boxes (Data screen, 20 boxes)...
echo Have that screen up on your monitor before continuing.
pause
python calibrate_postmatch.py postgame_dealt_team1_0 postgame_taken_team1_0 postgame_dealt_team1_1 postgame_taken_team1_1 postgame_dealt_team1_2 postgame_taken_team1_2 postgame_dealt_team1_3 postgame_taken_team1_3 postgame_dealt_team1_4 postgame_taken_team1_4 postgame_dealt_team2_0 postgame_taken_team2_0 postgame_dealt_team2_1 postgame_taken_team2_1 postgame_dealt_team2_2 postgame_taken_team2_2 postgame_dealt_team2_3 postgame_taken_team2_3 postgame_dealt_team2_4 postgame_taken_team2_4
pause
