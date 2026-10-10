@echo off
rem Start research assistant.cmd -- double-click to open Claude Code in this folder.
rem First run: sets the tool up (it reads every document once). Every later run: just ask.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 or newer is not installed, or was installed without "Add python.exe to PATH".
  echo Install it from python.org, tick that box, then run this again.
  pause
  exit /b 1
)
where claude >nul 2>nul
if errorlevel 1 (
  echo Claude Code is not installed. Install it, close this window and run this again.
  pause
  exit /b 1
)
if exist "engine\artefacts\READY" (
  claude --model claude-opus-5
) else (
  echo First run: the tool will set itself up now. It reads every document in this folder
  echo once, about five minutes per 1,000 files, and then it is ready for questions.
  echo Claude Code will ask permission to run a few setup commands. Answer yes to each.
  echo If Claude Code asks whether you trust this folder, answer yes.
  claude --model claude-opus-5 "initialize"
)
