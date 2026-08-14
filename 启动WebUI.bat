@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo    GPT 注册 WebUI 启动器（Windows）
echo ============================================
echo.

REM ---- 停止 5000 端口上的旧 WebUI 进程 ----
set "OLDPID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000" ^| findstr "LISTENING"') do set "OLDPID=%%a"
if defined OLDPID (
  echo 停止旧 WebUI 进程 PID=%OLDPID% ...
  taskkill /F /PID %OLDPID% >nul 2>&1
  timeout /t 2 /nobreak >nul
)

echo 启动 WebUI: http://127.0.0.1:5000
start "GPT-WebUI" /min cmd /c ".venv\Scripts\python web.py --host 127.0.0.1 --port 5000"
timeout /t 4 /nobreak >nul

REM ---- 检查是否启动成功 ----
set "NEWPID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000" ^| findstr "LISTENING"') do set "NEWPID=%%a"
if defined NEWPID (
  echo.
  echo 启动成功！ PID=%NEWPID%
  echo 访问 http://127.0.0.1:5000
  start http://127.0.0.1:5000
) else (
  echo.
  echo 启动失败，请查看 logs\webui.log
)
echo.
pause
