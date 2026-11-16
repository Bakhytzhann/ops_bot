@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Project Python missing. Create .venv and install requirements-dev-agent.txt.
  exit /b 1
)
".venv\Scripts\python.exe" -m dev_agent.main %*
if errorlevel 1 (
  echo Dev Agent failed. Check configuration and logs\dev_agent.log.
  exit /b 1
)
