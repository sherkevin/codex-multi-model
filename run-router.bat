@echo off
rem Windows: start the router in the foreground (closing this window stops it).
rem For an always-on install use service\install-windows-service.ps1 instead.
rem
rem NOTE: this file is intentionally ASCII-only. cmd.exe parses batch files with
rem the OEM code page, so non-ASCII text here gets garbled on many systems.
rem Chinese docs: README.zh.md
rem
rem The upstream API keys must be present as environment variables, otherwise the
rem router answers with "missing XXX_API_KEY".
rem   temporary:  set EXAMPLE_GATEWAY_API_KEY=your-key     (this window only)
rem   permanent:  setx EXAMPLE_GATEWAY_API_KEY "your-key"  (new windows only)

setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] python not found. Install Python 3.9+ and tick "Add to PATH".
  exit /b 1
)

if "%CODEX_HOME%"=="" set CODEX_HOME=%USERPROFILE%\.codex
if "%CODEX_ROUTER_PORT%"=="" set CODEX_ROUTER_PORT=8317

echo codex-model-router -^> http://127.0.0.1:%CODEX_ROUTER_PORT%/v1/responses
echo CODEX_HOME=%CODEX_HOME%
echo Press Ctrl+C to stop.

rem Log to a file as well, so the run is inspectable afterwards.
if "%ROUTER_LOG_FILE%"=="" set ROUTER_LOG_FILE=%TEMP%\codex-model-router.log
python codex-model-router.py

endlocal
