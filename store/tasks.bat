@echo off
REM ===========================================================================
REM  tasks.bat - what each run does. Started by run.bat after the update.
REM
REM  Safe to change in any future version: it is always read fresh, after
REM  update.bat has finished pulling the latest code.
REM
REM  Current version: build the daily and monthly reports and upload them.
REM  Running every 30 minutes keeps today's report current through the day;
REM  the uploader skips anything the server already has.
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

REM Each step runs even if the one before it failed: if the device is
REM unreachable, reports already on disk still get uploaded.
REM "call" is required: if python is a batch-file wrapper (some installs use
REM one), running it without call would end tasks.bat after the first step.
call %PY% attendance_report.py >> "%LOG%" 2>&1
if errorlevel 1 echo [%DATE% %TIME:~0,8%] attendance_report.py FAILED>> "%LOG%"

call %PY% monthly_report.py >> "%LOG%" 2>&1
if errorlevel 1 echo [%DATE% %TIME:~0,8%] monthly_report.py FAILED>> "%LOG%"

call %PY% upload_reports.py >> "%LOG%" 2>&1
if errorlevel 1 echo [%DATE% %TIME:~0,8%] upload_reports.py FAILED>> "%LOG%"

echo [%DATE% %TIME:~0,8%] ---- run finished ---->> "%LOG%"
exit /b 0
