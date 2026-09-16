@echo off
rem The `carto` every host sees on PATH. Windows twin of `launcher/carto`;
rem see that file for why the payload is resolved through a pointer file
rem instead of an absolute path baked in at install time.
setlocal EnableDelayedExpansion

set "HERE=%~dp0"
set "POINTER=%HERE%..\runtime.path"
if not exist "%POINTER%" goto :nopayload
set /p PAYLOAD=<"%POINTER%"
if not exist "%PAYLOAD%\runtime\carto.exe" goto :nopayload

rem The grammar pack fetches parser libraries on first use. On a default-deny
rem machine that never succeeds, so the bundled copy is named explicitly —
rem unless the operator has already seeded a cache of their own.
if "%TREE_SITTER_LANGUAGE_PACK_CACHE_DIR%"=="" (
    set "TREE_SITTER_LANGUAGE_PACK_CACHE_DIR=%PAYLOAD%\grammars"
)

"%PAYLOAD%\runtime\carto.exe" %*
exit /b %ERRORLEVEL%

:nopayload
echo carto: no engine payload registered at %POINTER% 1>&2
echo carto: reinstall the Cartograph VS Code extension, or re-run installer\install.ps1 1>&2
exit /b 127
