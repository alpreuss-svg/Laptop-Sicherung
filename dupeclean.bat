@echo off
rem DupeClean - Startdatei. Aufruf z. B.:  dupeclean scan E:\Sicherung
setlocal
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0dupeclean.py" %*
) else (
    py -3 "%~dp0dupeclean.py" %*
)
exit /b %errorlevel%
