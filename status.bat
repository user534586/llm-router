@echo off
chcp 65001 >nul
echo === LLM Router Status ===
echo.

echo [1] Port 8100 listeners:
netstat -ano | findstr ":8100" | findstr "LISTENING"
if errorlevel 1 echo     (not listening)
echo.

echo [2] Health:
curl -s -m 3 http://127.0.0.1:8100/health
echo.
echo.

echo [3] Last 15 log lines:
if exist "%~dp0logs\router.log" (
    powershell -NoProfile -Command "Get-Content '%~dp0logs\router.log' -Tail 15"
) else (
    echo     (no log yet)
)
echo.
pause
