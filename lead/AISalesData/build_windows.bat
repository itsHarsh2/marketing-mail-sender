@echo off
REM ===================================================================
REM  build_windows.bat - build AISalesMachine.exe
REM
REM  RUN THIS ON A WINDOWS MACHINE. PyInstaller bundles the host
REM  platform's Python runtime and cannot cross-compile, so a Windows
REM  .exe cannot be produced from Linux or macOS.
REM
REM  Requirements:
REM    - Windows 10/11 64-bit
REM    - Python 3.12 from python.org (tick "Add Python to PATH")
REM    - About 3 GB free disk space
REM
REM  Usage:  double-click this file, or run it from a terminal.
REM ===================================================================

setlocal
cd /d "%~dp0"

echo.
echo ============================================================
echo   Building AI Sales Machine for Windows
echo ============================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not on PATH.
    echo Install Python 3.12 from https://www.python.org/downloads/
    echo and tick "Add Python to PATH" during setup.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo Using Python %PYVER%
echo.

echo [1/4] Creating build environment...
if not exist build-venv (
    python -m venv build-venv || goto :fail
)
call build-venv\Scripts\activate.bat

echo [2/4] Installing dependencies (several minutes on first run)...
python -m pip install --upgrade pip --quiet || goto :fail
pip install -r requirements.txt --quiet || goto :fail
pip install pyinstaller --quiet || goto :fail

echo [3/4] Cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [4/4] Packaging (this takes 5-15 minutes)...
pyinstaller AISalesMachine.spec --noconfirm --clean || goto :fail

echo.
echo ============================================================
echo   BUILD COMPLETE
echo ============================================================
echo.
echo   Output folder:  dist\AISalesMachine\
echo   Run it with  :  dist\AISalesMachine\AISalesMachine.exe
echo.
echo   To share it, zip the WHOLE AISalesMachine folder.
echo   The .exe alone will not run - it needs the files beside it.
echo.
echo   The recipient sets their own password on first launch;
echo   do NOT ship your .streamlit\secrets.toml with it.
echo.
pause
exit /b 0

:fail
echo.
echo BUILD FAILED - see the messages above.
pause
exit /b 1
