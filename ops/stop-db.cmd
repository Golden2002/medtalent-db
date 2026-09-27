@echo off
REM ============================================================================
REM  ops\stop-db.cmd -- stop local PostgreSQL (idempotent)
REM  NOTE: keep this file ASCII-only (cmd.exe uses the OEM codepage).
REM ============================================================================
setlocal
set "PGBIN=D:\wbo-workspace\.tools\pgsql\bin"
set "PGDATA=D:\wbo-workspace\.tools\pgdata"

"%PGBIN%\pg_ctl.exe" -D "%PGDATA%" status >nul 2>&1
if not %errorlevel%==0 (
  echo [=] PostgreSQL is not running
  exit /b 0
)

echo [*] Stopping PostgreSQL ...
"%PGBIN%\pg_ctl.exe" -D "%PGDATA%" -m fast -w stop
exit /b %errorlevel%
