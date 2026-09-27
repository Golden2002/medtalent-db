@echo off
REM ===========================================================================
REM  MedTalent - start both sites for manual verification (ASCII only: cmd.exe
REM  parses .cmd in the OEM codepage, Chinese comments get mangled).
REM ===========================================================================
setlocal
cd /d "%~dp0.."
python code\demo\serve_all.py %*
if errorlevel 1 (
  echo.
  echo [X] serve_all.py failed. See output above.
  pause
  exit /b 1
)
endlocal
