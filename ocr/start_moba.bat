@echo off
cd /d "%~dp0moba"
echo Starting MOBA OCR engine + relay server...
python ocr_engine.py
pause
