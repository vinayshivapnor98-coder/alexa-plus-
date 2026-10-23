@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run start_web.bat first to create the environment and install dependencies.
  pause
  exit /b 1
)
echo Starting MCP tools server on http://127.0.0.1:8000/mcp ...
".venv\Scripts\python.exe" server.py
endlocal
