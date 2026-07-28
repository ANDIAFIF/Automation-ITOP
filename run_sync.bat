@echo off
rem ============================================================
rem Runner sync agent iTop -> Dashboard untuk PC bridge Windows.
rem Dipanggil Task Scheduler / folder Startup (lihat README bag. 4).
rem Auto-restart: crash apa pun -> tunggu 30 detik -> jalan lagi.
rem ============================================================
cd /d "%~dp0"

:restart
".venv\Scripts\python.exe" sync.py --loop
echo [%date% %time%] sync.py berhenti (exit %errorlevel%) - restart 30 detik lagi...
timeout /t 30 /nobreak >nul
goto restart
