@echo off
chcp 65001 >nul
cd /d %~dp0
echo Starting LLM Router (detached)...
python daemon.py
echo.
echo Router should now be running on http://127.0.0.1:8100
echo Check logs\router.log for activity.
echo.
echo Health check:
timeout /t 2 /nobreak >nul
curl -s http://127.0.0.1:8100/health
echo.
pause
