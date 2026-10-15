@echo off
REM ===========================================================================
REM  Stops both containers. The DATA IS KEPT, so windows-start.cmd will come
REM  back up in seconds instead of reloading everything.
REM
REM  To wipe the data too, see the last lines of this file.
REM ===========================================================================

echo Stopping...
docker rm -f modus-app >nul 2>&1
docker rm -f modus-db  >nul 2>&1
echo Stopped. Data is kept - run windows-start.cmd to come back up.
echo.
echo To erase the data as well, run BOTH of these together:
echo     docker volume rm modus_pgdata modus_blobs
echo.
echo Erase both or neither. Removing only one leaves the database pointing at
echo evidence files that no longer exist, and images will 404 permanently.
echo.
pause
