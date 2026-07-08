@echo off
title Session_Recall Web UI  (close this window to stop)
cd /d "%~dp0"
echo Starting Session_Recall Web UI...
echo A browser tab will open at http://127.0.0.1:8765/
echo.
echo Leave this window open. Close it, or press Ctrl+C, to stop the server.
echo.
".venv\Scripts\python.exe" -m session_recall.webui
echo.
echo Server stopped.
pause
