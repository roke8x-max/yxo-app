@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM 首次启动：自动创建运行环境（不影响系统 Python）
if not exist "venv\Scripts\python.exe" (
  echo 首次启动，正在创建运行环境，请稍候...
  python -m venv venv
  call venv\Scripts\pip install -q flask openpyxl
)

REM 检查端口是否已被占用（例如程序已经在运行）
set STATUS=CLOSED
for /f %%i in ('venv\Scripts\python.exe check_port.py') do set STATUS=%%i

if "%STATUS%"=="OPEN" (
  echo.
  echo ============================================
  echo   程序已经在运行了！
  echo   正在打开浏览器：http://localhost:5011
  echo ============================================
  start "" http://localhost:5011
  timeout /t 2 >nul
  exit /b
)

echo 正在启动 YXO 订舱数据管理...
start "" venv\Scripts\python.exe app.py
timeout /t 3 >nul
echo.
echo ============================================
echo   已启动！请用浏览器打开：
echo   http://localhost:5011
echo   （同事通过 nginx 反代访问 http://你们的地址:5000/yxo/ ）
echo ============================================
start "" http://localhost:5011
echo 关闭：直接关掉弹出的 python 窗口即可。
pause
