@echo off
title BLUEPRINT - App Teardown Engine
cd /d "%~dp0"
echo ============================================================
echo   BLUEPRINT  -  reverse-engineer any app from public signals
echo ============================================================
echo.
echo   Starting server...  keep THIS window open while using it.
echo   When it says "Uvicorn running", open:
echo.
echo        http://127.0.0.1:8975
echo.
echo   Enter a site (e.g. linear.app), optionally tick "Deep recon",
echo   and click Analyze. Close this window to stop the server.
echo ============================================================
echo.
python app.py
echo.
echo (server stopped)
pause
