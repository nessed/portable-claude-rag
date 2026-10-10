@echo off
rem hooks.cmd -- the one command the shipped .claude\settings.json and skills point at.
rem Lives at engine\bin\hooks.cmd, so the folder that holds engine\ is two levels up and
rem nothing here is an absolute path: the folder can be moved.
rem
rem   hooks.cmd prompt    UserPromptSubmit: print the one-line briefing
rem   hooks.cmd session   SessionStart: recount files, print the briefing
rem   hooks.cmd guard     PreToolUse: refuse writes outside engine\ while ON
rem   hooks.cmd stop1     Stop: the citation guard
rem   hooks.cmd stop2     Stop: the note/figure verifier
rem   hooks.cmd on|off    the switch (no Python needed)
rem   hooks.cmd status    for /rag-status
rem
rem Before `initialize` there is no Python environment; every event then exits 0 so the
rem first session never sees a hook error.
setlocal
set "ROOT=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "PY=%ROOT%\engine\.venv\Scripts\python.exe"
set "BIN=%ROOT%\engine\bin"
set "EV=%~1"

if "%EV%"=="off" goto :off
if "%EV%"=="on" goto :on

if not exist "%ROOT%\engine\artefacts\READY" goto :notsetup
if exist "%ROOT%\engine\OFF" goto :isoff
if not exist "%PY%" goto :notsetup

if "%EV%"=="guard" goto :guard
if "%EV%"=="stop1" goto :stop1
if "%EV%"=="stop2" goto :stop2
if "%EV%"=="prompt" goto :py
if "%EV%"=="session" goto :py
if "%EV%"=="status" goto :py
exit /b 0

:py
"%PY%" "%BIN%\c_mode.py" %EV%
exit /b 0

:guard
"%PY%" "%BIN%\c_mode.py" guard
if errorlevel 1 (
  echo research assistant: the file guard could not run, so this tool call is refused to be safe. 1>&2
  exit /b 2
)
exit /b 0

:stop1
"%PY%" "%BIN%\c_stop_guard.py" --v2 --require-header "(research assistant on)" --ban-phrases --sources-last --log-dir "%ROOT%\engine\artefacts\hooks"
exit /b 0

:stop2
"%PY%" "%BIN%\c_verify_guard.py" --log-dir "%ROOT%\engine\artefacts\hooks"
exit /b 0

:off
if not exist "%ROOT%\engine" mkdir "%ROOT%\engine"
echo off> "%ROOT%\engine\OFF"
echo research assistant: OFF. Plain Claude Code now; /rag-on switches it back.
exit /b 0

:on
if exist "%ROOT%\engine\OFF" del "%ROOT%\engine\OFF"
if exist "%ROOT%\engine\artefacts\READY" (
  echo research assistant: ON.
) else (
  echo research assistant: not set up yet. Say initialize.
)
exit /b 0

:notsetup
if "%EV%"=="prompt" echo [research assistant: NOT SET UP] When the user says initialize, read engine/INITIALIZE.md and follow it, start to finish.
if "%EV%"=="session" echo [research assistant: NOT SET UP] When the user says initialize, read engine/INITIALIZE.md and follow it, start to finish.
if "%EV%"=="status" echo state: NOT SET UP. Say initialize.
if "%EV%"=="guard" echo {}
if "%EV%"=="stop1" echo {}
if "%EV%"=="stop2" echo {}
exit /b 0

:isoff
if "%EV%"=="prompt" echo [research assistant: OFF] Plain Claude Code: the document index is not consulted and file changes are allowed if the user approves them. /rag-on switches it back on; /ask ^<question^> uses it for one question.
if "%EV%"=="session" echo [research assistant: OFF] Plain Claude Code: the document index is not consulted and file changes are allowed if the user approves them. /rag-on switches it back on; /ask ^<question^> uses it for one question.
if "%EV%"=="status" echo state: OFF. /rag-on switches it on.
if "%EV%"=="guard" echo {}
if "%EV%"=="stop1" echo {}
if "%EV%"=="stop2" echo {}
exit /b 0
