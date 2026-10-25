@echo off
rem DupeClean - Startdatei. Aufruf z. B.:  dupeclean scan E:\Sicherung
rem Per Doppelklick: Fensteroberflaeche.  'dupeclean konsole': Eingabefenster fuer Befehle.
setlocal
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYEXE=python"
where py >nul 2>nul && set "PYEXE=py -3"
if "%~1"=="" goto oberflaeche
if /i "%~1"=="konsole" goto interaktiv
%PYEXE% "%~dp0dupeclean.py" %*
exit /b %errorlevel%

:oberflaeche
set "PYWEXE=pythonw"
where pyw >nul 2>nul && set "PYWEXE=pyw -3"
start "" %PYWEXE% "%~dp0dupeclean_gui.pyw"
exit /b 0

:interaktiv
set "PATH=%~dp0;%PATH%"
cd /d "%USERPROFILE%"
title DupeClean
doskey dupeclean=%PYEXE% "%~dp0dupeclean.py" $*
doskey scan=%PYEXE% "%~dp0dupeclean.py" scan $*
doskey report=%PYEXE% "%~dp0dupeclean.py" report $*
doskey apply=%PYEXE% "%~dp0dupeclean.py" apply $*
doskey undo=%PYEXE% "%~dp0dupeclean.py" undo $*
doskey purge=%PYEXE% "%~dp0dupeclean.py" purge $*
doskey runs=%PYEXE% "%~dp0dupeclean.py" runs $*
%PYEXE% "%~dp0dupeclean.py" --help
echo.
echo ----------------------------------------------------------------------
echo  Dieses Fenster bleibt offen. Befehle hier eintippen, zum Beispiel:
echo.
echo      dupeclean scan E:\Sicherung
echo      dupeclean report
echo      dupeclean apply "E:\.dupeclean\berichte\...\loeschplan.csv"
echo.
echo  Das Wort dupeclean darf auch fehlen, z. B.:  scan E:\Sicherung
echo.
echo  Hilfe zu einem Befehl:   dupeclean scan --help
echo  Fenster schliessen:      exit
echo ----------------------------------------------------------------------
echo.
cmd /k
