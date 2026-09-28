@echo off
REM ===========================================================================
REM  update.bat - pull the latest code from GitHub.
REM
REM  Double-click it, or schedule it in Task Scheduler a few minutes before
REM  the daily report task so every store picks up changes on its own.
REM
REM  Safe by design:
REM    * store_config.ini, reports and logs are ignored by git, so an update
REM      never touches them.
REM    * --ff-only means it only ever fast-forwards. If this PC's copy of the
REM      code was edited by hand, it refuses rather than merging, and says so.
REM ===========================================================================

setlocal
set "STORE_DIR=%~dp0"
set "LOG=%STORE_DIR%update_log.txt"
cd /d "%STORE_DIR%.."

REM --- find git: Task Scheduler often runs without the user's PATH -----------
set "GIT=git"
where git >nul 2>&1
if errorlevel 1 (
    if exist "%ProgramFiles%\Git\cmd\git.exe" (
        set "GIT=%ProgramFiles%\Git\cmd\git.exe"
    ) else if exist "%LOCALAPPDATA%\Programs\Git\cmd\git.exe" (
        set "GIT=%LOCALAPPDATA%\Programs\Git\cmd\git.exe"
    ) else (
        call :log "FAILED: git not found. Install Git for Windows from git-scm.com."
        exit /b 1
    )
)

call :log "---- update started ----"

REM "call" in front of every git command: if git is a batch-file wrapper
REM rather than git.exe, running it without call would end this script.

REM --- refuse to pull over hand-edited code files ----------------------------
call "%GIT%" diff --quiet 2>nul
if errorlevel 1 (
    call :log "FAILED: code files in this folder were edited by hand, which blocks updates."
    call :log "Settings belong in store\store_config.ini, not in the .py files."
    call :log "To discard the edits and update:  git checkout -- .   then run this again."
    call "%GIT%" status --short >> "%LOG%" 2>&1
    exit /b 1
)

call "%GIT%" pull --ff-only >> "%LOG%" 2>&1
if errorlevel 1 (
    call :log "FAILED: git pull did not complete - see the lines above."
    findstr /C:"dubious ownership" "%LOG%" >nul 2>&1 && (
        call :log "Fix for 'dubious ownership': run this once, then try again:"
        call :log "  git config --global --add safe.directory *"
    )
    exit /b 1
)

for /f "delims=" %%c in ('"%GIT%" log -1 --format^="%%h %%s (%%cr)"') do call :log "Now at: %%c"
call :log "---- update finished ----"
exit /b 0

:log
echo [%DATE% %TIME:~0,8%] %~1
echo [%DATE% %TIME:~0,8%] %~1>> "%LOG%"
exit /b 0
