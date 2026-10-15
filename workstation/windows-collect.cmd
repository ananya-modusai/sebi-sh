@echo off
setlocal
REM ===========================================================================
REM  When something breaks on Windows:  windows-collect.cmd
REM
REM  Writes everything needed to diagnose into one text file and opens it in
REM  Notepad. Select all, copy, and send it. Passwords are not printed.
REM ===========================================================================

set "OUT=%USERPROFILE%\Desktop\modus-report.txt"

echo Collecting... this takes about 20 seconds.

(
echo =========== MODUS WINDOWS REPORT  %DATE% %TIME% ===========
echo.
echo --- docker ---
docker version --format "client {{.Client.Version}} / server {{.Server.Version}}" 2>^&1
echo.
echo --- containers ---
docker ps -a --format "{{.Names}} | {{.Image}} | {{.Status}} | {{.Ports}}" 2>^&1
echo.
echo --- images ---
docker images --format "{{.Repository}}:{{.Tag}} | {{.Size}}" 2>^&1
echo.
echo --- volumes ---
docker volume ls 2>^&1
echo.
echo --- http checks ---
echo UI:
curl -s -o nul -w "  http://localhost:3000/ -^> %%{http_code}\n" http://localhost:3000/ 2>^&1
echo API:
curl -s -o nul -w "  http://localhost:3000/docs -^> %%{http_code}\n" http://localhost:3000/docs 2>^&1
echo DATA ^(401 is correct^):
curl -s -o nul -w "  http://localhost:3000/api/companies -^> %%{http_code}\n" http://localhost:3000/api/companies 2>^&1
echo.
echo --- database contents ---
docker exec modus-db psql -U modus -d surveillance -tAc "SELECT 'tables='^|^|count(*) FROM information_schema.tables WHERE table_schema='surveillance_sebi'" 2>^&1
docker exec modus-db psql -U modus -d surveillance -tAc "SELECT 'companies='^|^|count(*) FROM surveillance_sebi.companies" 2>^&1
docker exec modus-db psql -U modus -d surveillance -tAc "SELECT 'users_with_passwords='^|^|count(*) FROM public.users WHERE hashed_password IS NOT NULL" 2>^&1
echo.
echo --- modus-app : last 60 log lines ---
docker logs --tail 60 modus-app 2>^&1
echo.
echo --- modus-app : errors ---
docker logs modus-app 2>^&1 ^| findstr /I "error exception fatal refused denied timeout"
echo.
echo --- modus-db : last 40 log lines ---
docker logs --tail 40 modus-db 2>^&1
echo.
echo --- modus-db : errors ---
docker logs modus-db 2>^&1 ^| findstr /I "error fatal panic"
echo.
echo =========== END ===========
) > "%OUT%" 2>&1

echo.
echo ===========================================================
echo  Report written to your Desktop:
echo    modus-report.txt
echo.
echo  Opening it now. Select all ^(Ctrl+A^), copy ^(Ctrl+C^),
echo  and send it through WhatsApp to yourself.
echo ===========================================================
echo.
notepad "%OUT%"
