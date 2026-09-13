@echo off
rem ===================================================================
rem  Lend this machine to the queue. Double-click it, or run it.
rem
rem  Three things had to be right and in order, and getting any of them
rem  wrong failed in a different and unhelpful way: be in the repository
rem  (otherwise "can't open file ...\tools\volunteer.py"), have the
rem  broker tunnel open (otherwise "nothing is listening on 127.0.0.1:
rem  5672"), and use the interpreter that has cairo and Flask rather
rem  than whichever python is first on PATH (otherwise "No module named
rem  flask"). None of that is a decision. It is setup, so it lives here.
rem
rem  The tunnel opens in its own window and STAYS OPEN when this stops,
rem  deliberately: it is also how you reach the server, and closing
rem  somebody's ssh session because a render finished is rude.
rem ===================================================================
setlocal

rem -- The repository is wherever this file is, so a shortcut on the
rem    desktop works exactly as well as running it from here.
cd /d "%~dp0"

rem -- The interpreter. VSW_PYTHON wins if it is set; otherwise the conda
rem    environment that has cairo, Flask and the render engine. Bare
rem    `python` is the last resort and will almost certainly be wrong,
rem    which the preflight below says out loud rather than guessing at.
set "PY=%VSW_PYTHON%"
if not defined PY set "PY=%USERPROFILE%\.conda\envs\VideoScoreSync\python.exe"
if not exist "%PY%" set "PY=python"

set "WEBBOX=root@172.104.237.127"

echo.
echo   lending this machine
echo   --------------------
echo   repository   %CD%
echo   interpreter  %PY%
echo.

rem -- The tunnel. The broker is not on the public internet by design, so
rem    this is the only way to reach it. Started only if nothing already
rem    holds the port: a second tunnel fails with "bind: Address already
rem    in use" and looks like a broken setup rather than a working one.
rem
rem    BOTH the address AND "LISTENING". Matching the address alone finds
rem    it in the FOREIGN address column of a closed connection sitting in
rem    TIME_WAIT, so minutes after a tunnel is shut this would report one
rem    as open, decline to start it, and hand you the exact "nothing is
rem    listening on 127.0.0.1:5672" that this file exists to prevent.
netstat -an | findstr /c:"127.0.0.1:5672" | findstr /c:"LISTENING" >nul
if errorlevel 1 (
    echo   opening the broker tunnel in its own window...
    start "vsw broker tunnel - leave this open" ssh -N -L 5672:127.0.0.1:5672 %WEBBOX%
    rem  ssh needs a moment to authenticate and bind before the volunteer
    rem  tries to connect; without this the first run reports the broker
    rem  unreachable and the second works, for no visible reason.
    timeout /t 4 /nobreak >nul
) else (
    echo   the broker tunnel is already open.
)

echo.
"%PY%" tools\volunteer.py %*

echo.
echo   stopped taking work. The tunnel window is still open - close it
echo   yourself when you are done with the server.
echo.
pause
