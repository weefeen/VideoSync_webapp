@echo off
rem ===================================================================
rem  Open the dashboards. Double-click it, or run it.
rem
rem  Grafana listens on 127.0.0.1:3000 ON THE SERVER and is deliberately
rem  not published: it carries visitors' addresses, job records and the
rem  machine's own health, none of which belongs on the open internet.
rem  The only way in is therefore a tunnel, and a tunnel is setup rather
rem  than a decision, so it lives here.
rem
rem  The tunnel opens in its own window and STAYS OPEN when you close the
rem  browser, the same arrangement lend.bat uses and for the same reason:
rem  it is also how you reach the server, and closing somebody's ssh
rem  session because a browser tab shut is rude.
rem ===================================================================
setlocal

rem -- The repository is wherever this file is, so a shortcut on the
rem    desktop works exactly as well as running it from here.
cd /d "%~dp0"

set "WEBBOX=root@172.104.237.127"
set "PORT=3000"

echo.
echo   dashboards
echo   ----------
echo   server   %WEBBOX%
echo   address  http://localhost:%PORT%
echo.

rem -- Started only if nothing already holds the port: a second tunnel
rem    fails with "bind: Address already in use" and looks like a broken
rem    setup rather than a working one.
rem
rem    BOTH the address AND "LISTENING". Matching the address alone finds
rem    it in the FOREIGN address column of a closed connection sitting in
rem    TIME_WAIT, so minutes after a tunnel is shut this would report one
rem    as open and then hand you a browser tab that cannot connect.
netstat -an | findstr /c:"127.0.0.1:%PORT%" | findstr /c:"LISTENING" >nul
if errorlevel 1 (
    echo   opening the tunnel in its own window...
    start "vsw grafana tunnel - leave this open" ssh -N -L %PORT%:127.0.0.1:%PORT% %WEBBOX%
    rem  ssh needs a moment to authenticate and bind. Without this the
    rem  browser opens first, fails to connect, and you are looking at an
    rem  error page while the tunnel comes up behind it.
    timeout /t 4 /nobreak >nul
) else (
    echo   the tunnel is already open.
)

rem -- The browser. `start ""` with an empty title because the first
rem    quoted argument to `start` is the WINDOW TITLE, not the thing to
rem    run: `start "http://..."` opens an empty command prompt titled
rem    with the address and no browser at all.
echo   opening the browser...
start "" "http://localhost:%PORT%"

echo.
echo   Grafana is at http://localhost:%PORT% for as long as the tunnel
echo   window stays open. Close that window when you are done.
echo.
echo   If it asks you to sign in and you have not set a password, the
echo   first login is admin / admin and it will make you change it.
echo.
pause
