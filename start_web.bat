@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not exist ".venv\Scripts\python.exe" (
  echo Creating Python virtual environment...
  py -3.12 -m venv .venv 2>nul
  if errorlevel 1 py -m venv .venv
  if errorlevel 1 goto failed
)
echo Installing or checking project dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo.
echo Starting Alexa+ Personal Agent web interface...
echo Open http://127.0.0.1:5000 in Chrome or Edge.
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:5000"
".venv\Scripts\python.exe" web_app.py
goto end
:failed
echo.
echo Setup failed. Check the error above, Python installation, and internet connection.
pause
:end
endlocal
