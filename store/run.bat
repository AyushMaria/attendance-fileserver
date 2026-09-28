@echo off
REM ===========================================================================
REM  run.bat - the ONLY thing Task Scheduler runs. Set it up once:
REM      every 30 minutes, from opening time to one hour after closing.
REM
REM  It updates the code, then runs whatever tasks.bat says to do. When the
REM  system changes (new scripts, new schedule of work), only tasks.bat
REM  changes - and that arrives through update.bat, so the store never needs
REM  a visit or a Task Scheduler change again.
REM
REM  DO NOT EDIT OR CHANGE THIS FILE IN FUTURE VERSIONS.
REM  Windows reads a running batch file line by line from disk. If update.bat
REM  pulled a new version of THIS file while it runs, Windows would carry on
REM  reading the new file from the old position. tasks.bat is safe to change
REM  because it only starts after the update has finished.
REM ===========================================================================

call "%~dp0update.bat"
call "%~dp0tasks.bat"
