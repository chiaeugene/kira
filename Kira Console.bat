@echo off
title Kira Console
cd /d "%~dp0"
echo Starting Kira Console... your browser will open shortly.
python -m streamlit run app.py --server.headless false
pause
