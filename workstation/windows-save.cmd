@echo off
setlocal enabledelayedexpansion
REM ===========================================================================
REM  Package both images for transfer to the VMs.
REM
REM    Double-click this file, or:  windows-save.cmd
REM
REM  The VMs have no internet, so the images have to travel as files. This pulls
REM  them, writes two .tar.gz files, and prints a SHA256 for each so you can
REM  prove the transfer was not truncated.
REM
REM  Takes 15-25 minutes. The output is about 1.9 GB.
REM ===========================================================================

set "APP_IMAGE=olamzar/modus-app:2.4"
set "DB_IMAGE=olamzar/modus-db:1.3"
set "OUT=%USERPROFILE%\Desktop\modus-transfer"

echo ===========================================================
echo  Packaging images for the VMs. This takes 15-25 minutes.
echo  Output goes to:  %OUT%
echo ===========================================================
echo.

docker version >nul 2>&1
if errorlevel 1 (
  echo FAILED - Docker is not running. Start Docker Desktop and try again.
  goto :end
)

if not exist "%OUT%" mkdir "%OUT%"

call :package "%DB_IMAGE%"  "modus-db-1.3"
if errorlevel 1 goto :end
call :package "%APP_IMAGE%" "modus-app-2.4"
if errorlevel 1 goto :end

echo.
echo ===========================================================
echo  DONE. Two files in %OUT% :
echo.
dir /b "%OUT%\*.tar.gz"
echo.
echo  Checksums are in checksums.txt beside them.
echo.
echo  Next:
echo    1. Move BOTH .tar.gz files to the VMs with ARCON PAM
echo       - modus-db-1.3.tar.gz   to the DATABASE VM
echo       - modus-app-2.4.tar.gz  to the APPLICATION VM
echo    2. On each VM, confirm the file arrived intact:
echo         sha256sum modus-db-1.3.tar.gz
echo       It must match checksums.txt. If it does not, transfer it again -
echo       a truncated file fails to load with a confusing error.
echo    3. Put the scripts from the vm folder beside it and run them.
echo ===========================================================

:end
echo.
pause
exit /b 0

REM ---------------------------------------------------------------------------
:package
set "IMG=%~1"
set "BASE=%~2"

echo [%BASE%] checking for the image...
docker image inspect %IMG% >nul 2>&1
if errorlevel 1 (
  echo        pulling %IMG% ...
  docker pull %IMG%
  if errorlevel 1 ( echo        FAILED to pull %IMG% & exit /b 1 )
) else (
  echo        already present
)

echo [%BASE%] writing the tar ^(slow, no progress shown^)...
docker save -o "%OUT%\%BASE%.tar" %IMG%
if errorlevel 1 ( echo        FAILED to save & exit /b 1 )

echo [%BASE%] compressing...
REM .NET's gzip - no extra tools needed, and podman load reads it directly.
powershell -NoProfile -Command ^
  "$i=[IO.File]::OpenRead('%OUT%\%BASE%.tar'); $o=[IO.File]::Create('%OUT%\%BASE%.tar.gz'); $g=New-Object IO.Compression.GzipStream($o,[IO.Compression.CompressionMode]::Compress); $i.CopyTo($g); $g.Dispose(); $o.Dispose(); $i.Dispose()"
if errorlevel 1 ( echo        FAILED to compress & exit /b 1 )
del "%OUT%\%BASE%.tar"

echo [%BASE%] checksum...
for /f "skip=1 tokens=1" %%h in ('certutil -hashfile "%OUT%\%BASE%.tar.gz" SHA256 ^| findstr /r "^[0-9a-f]"') do (
  echo %BASE%.tar.gz  %%h>> "%OUT%\checksums.txt"
  echo        %%h
  goto :done_hash
)
:done_hash
echo [%BASE%] ready
echo.
exit /b 0
