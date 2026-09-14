@echo off
rem ===================================================================
rem  Who came to the site. Double-click it, or run it.
rem
rem  RUNS ON THE SERVER, because that is where the web server's log is.
rem  This opens one ssh session, runs the report there, and prints it
rem  here. No tunnel is left behind and nothing is copied down.
rem
rem  It answers a different question from the job records: those know
rem  who UPLOADED, and most people who open a page never upload
rem  anything, so they cannot tell you how many came.
rem ===================================================================
setlocal

cd /d "%~dp0"

set "WEBBOX=root@172.104.237.127"
set "REMOTE=/srv/vsw/current"

echo.
echo   visitors to chopin.weefeen.com
echo   ------------------------------
echo.

rem -- Arguments pass straight through: --where groups by country,
rem    --bots shows what the scanners asked for, --json is for a script.
ssh %WEBBOX% "cd %REMOTE% && /srv/vsw/venv/bin/python tools/pageviews.py %*"

echo.
echo   A machine that rents its address is not counted. Most of what
echo   reaches a new site is crawlers that found it in the public
echo   certificate log, and they claim to be Chrome.
echo.
pause
