@echo off
REM cc-l10n - Claude Code system prompt localization patch tool (Windows wrapper)
REM Forwards to the cross-platform Python entry cc_l10n.py.

setlocal
set "SCRIPT_DIR=%~dp0"
python "%SCRIPT_DIR%cc_l10n.py" %*
exit /b %ERRORLEVEL%
