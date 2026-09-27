@echo off
REM ============================================================================
REM  ops\start-db.cmd -- start local PostgreSQL (idempotent, safe to re-run)
REM  NOTE: keep this file ASCII-only. cmd.exe parses .cmd with the OEM codepage
REM        (GBK on this machine), so UTF-8 Chinese text here breaks parsing.
REM  Exit code: 0 = already running or started OK; non-zero = failed
REM ============================================================================
setlocal
set "PGBIN=D:\wbo-workspace\.tools\pgsql\bin"
set "PGDATA=D:\wbo-workspace\.tools\pgdata"

if not exist "%PGBIN%\pg_ctl.exe" (
  echo [X] PostgreSQL not found. Run: python ops\install_postgres.py
  exit /b 2
)

"%PGBIN%\pg_ctl.exe" -D "%PGDATA%" status >nul 2>&1
if %errorlevel%==0 (
  echo [=] PostgreSQL already running
  exit /b 0
)

echo [*] Starting PostgreSQL ...
"%PGBIN%\pg_ctl.exe" -D "%PGDATA%" -l "%PGDATA%\server.log" -w -t 60 start
if %errorlevel%==0 (
  echo [OK] started
  exit /b 0
) else (
  echo [X] start failed, see "%PGDATA%\server.log"
  exit /b 1
)
