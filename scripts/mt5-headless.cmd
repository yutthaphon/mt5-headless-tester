@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "PY_SCRIPT=%SCRIPT_DIR%mt5_headless.py"

rem --- Silently load .env (if present) into process env vars without echoing ---
set "ENV_FILE="
if defined MT5_ENV_FILE (
    if exist "%MT5_ENV_FILE%" set "ENV_FILE=%MT5_ENV_FILE%"
)
if not defined ENV_FILE (
    if exist ".env" set "ENV_FILE=.env"
)
if defined ENV_FILE (
    for /f "usebackq tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
        call :SetEnvIfUnset "%%~A" "%%B"
    )
)

goto :AfterEnvHelper

:SetEnvIfUnset
set "_K=%~1"
for /f "tokens=* delims= " %%K in ("%_K%") do set "_K=%%K"
if "%_K%"=="" goto :eof
if "%_K:~0,1%"=="#" goto :eof
if /i "%_K:~0,7%"=="export " set "_K=%_K:~7%"
for /f "tokens=* delims= " %%K in ("%_K%") do set "_K=%%K"
if "%_K%"=="" goto :eof
if defined %_K% goto :eof
set "_V=%~2"
if "%_V%"=="" goto :eof
set "%_K%=%_V%"
goto :eof

:AfterEnvHelper
if /i "%~1"=="--bootstrap-python" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Invoke-MT5Headless.ps1" %*
    exit /b %ERRORLEVEL%
)

call :ResolvePython
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Invoke-MT5Headless.ps1" %*
    exit /b %ERRORLEVEL%
)

if "%~1"=="" (
    "%MT5_PYTHON%" "%PY_SCRIPT%" --help
    exit /b %ERRORLEVEL%
)

if /i "%~1"=="--launch-minimized" goto :LaunchMinimized

"%MT5_PYTHON%" "%PY_SCRIPT%" %*
exit /b %ERRORLEVEL%

:ResolvePython
if defined MT5_PYTHON (
    "%MT5_PYTHON%" -c "import sys" >nul 2>&1
    if not errorlevel 1 exit /b 0
)
if exist "%SCRIPT_DIR%..\.python\python.exe" (
    "%SCRIPT_DIR%..\.python\python.exe" -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set "MT5_PYTHON=%SCRIPT_DIR%..\.python\python.exe"
        exit /b 0
    )
)
if exist ".python\python.exe" (
    ".python\python.exe" -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set "MT5_PYTHON=.python\python.exe"
        exit /b 0
    )
)
for /f "delims=" %%P in ('where python 2^>nul') do (
    echo %%P | findstr /I "WindowsApps" >nul
    if errorlevel 1 (
        "%%P" -c "import sys" >nul 2>&1
        if not errorlevel 1 (
            set "MT5_PYTHON=%%P"
            exit /b 0
        )
    )
)
exit /b 1

:LaunchMinimized
shift
set "INI_PATH=%~1"
if "%INI_PATH%"=="" (
    echo ERROR: Specify ^<ini_path^> after --launch-minimized 1>&2
    exit /b 2
)
if not exist "%INI_PATH%" (
    echo ERROR: Tester INI not found: %INI_PATH% 1>&2
    exit /b 2
)
shift

set "PORTABLE_FLAG="
set "TERM_EXE=%MT5_TERMINAL_EXE%"
:ParseLaunchArgs
if "%~1"=="" goto :CheckLaunch
if /i "%~1"=="--portable" (
    set "PORTABLE_FLAG=/portable"
    shift
    goto :ParseLaunchArgs
)
if /i "%~1"=="--terminal-exe" (
    set "TERM_EXE=%~2"
    shift
    shift
    goto :ParseLaunchArgs
)
shift
goto :ParseLaunchArgs

:CheckLaunch
if not defined TERM_EXE (
    if defined MT5_TERMINAL_DIR (
        set "TERM_EXE=%MT5_TERMINAL_DIR%\terminal64.exe"
    ) else if defined PORTABLE_FLAG (
        if exist ".mt5-portable\terminal64.exe" (
            set "TERM_EXE=%CD%\.mt5-portable\terminal64.exe"
        ) else (
            set "TERM_EXE=C:\Program Files\MetaTrader 5\terminal64.exe"
        )
    ) else (
        set "TERM_EXE=C:\Program Files\MetaTrader 5\terminal64.exe"
    )
)
if not exist "%TERM_EXE%" (
    echo ERROR: terminal64.exe not found: %TERM_EXE% 1>&2
    exit /b 2
)

if not defined PORTABLE_FLAG (
    tasklist /FI "IMAGENAME eq terminal64.exe" /NH 2>nul | find /I "terminal64.exe" >nul
    if not errorlevel 1 (
        echo ERROR: terminal64.exe is running. Close MT5 gracefully via UI first; NEVER kill terminal64.exe. 1>&2
        exit /b 1
    )
)

for %%F in ("%INI_PATH%") do set "ABS_INI=%%~fF"
if defined PORTABLE_FLAG (
    start "" /MIN /WAIT "%TERM_EXE%" /portable "/config:%ABS_INI%"
) else (
    start "" /MIN /WAIT "%TERM_EXE%" "/config:%ABS_INI%"
)
set "MT5_EXIT=%ERRORLEVEL%"
"%MT5_PYTHON%" "%PY_SCRIPT%" scrub-ini "%ABS_INI%" >nul 2>&1
echo Exit     : %MT5_EXIT%  (not a verdict; inspect report and logs)
exit /b %MT5_EXIT%
