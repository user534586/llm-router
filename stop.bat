@echo off
chcp 65001 >nul
echo Stopping LLM Router...

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8100" ^| findstr "LISTENING"') do (
    echo Killing PID %%a
    taskkill /F /PID %%a >nul 2>&1
)

echo Done.
pause
