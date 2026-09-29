@echo off
REM ===========================================================================
REM  tasks.bat - what each run does. Started by run.bat after the update.
REM
REM  Safe to change in any future version: it is always read fresh, after
REM  update.bat has finished pulling the latest code.
REM
REM  Current version: send the device's staff list and punches to the
REM  website (sync.py). The website works out the calendars from them.
REM  Every 30 minutes keeps today's view current through the day.
REM ===========================================================================

setlocal
cd /d "%~dp0"
set "LOG=%~dp0run_log.txt"

REM Staff names on the device may contain non-English characters; without
REM this, printing one into the log could stop a script on Windows.
set "PYTHONIOENCODING=utf-8"

REM Task Scheduler often runs without the user's PATH. Prefer the py launcher
REM (installed into C:\Windows, so it is found even when PATH is broken).
set "PY=python"
where py >nul 2>&1 && set "PY=py"

echo.>> "%LOG%"
echo [%DATE% %TIME:~0,8%] ---- run started (using %PY%) ---->> "%LOG%"

REM "call" is required: if python is a batch-file wrapper (some installs use
REM one), running it without call would end tasks.bat at that line.
call %PY% sync.py >> "%LOG%" 2>&1
if errorlevel 1 echo [%DATE% %TIME:~0,8%] sync.py FAILED>> "%LOG%"

echo [%DATE% %TIME:~0,8%] ---- run finished ---->> "%LOG%"
exit /b 0
