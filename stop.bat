@echo off
chcp 65001 >nul
cd /d %~dp0
echo Stopping LLM Router...

set "PID="
if exist "router.pid" set /p PID=<"router.pid"

if defined PID (
    echo Killing PID %PID% (from router.pid)
    taskkill /F /PID %PID% >nul 2>&1
    del /F /Q "router.pid" >nul 2>&1
) else (
    echo No router.pid found - falling back to port 8100.
    echo (edit server.port in config.json? then kill it manually)
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8100" ^| findstr "LISTENING"') do (
        echo Killing PID %%a
        taskkill /F /PID %%a >nul 2>&1
    )
)

echo Done.
pause
