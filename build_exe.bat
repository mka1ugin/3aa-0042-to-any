@echo off
setlocal
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller
if errorlevel 1 exit /b 1

python -m PyInstaller --noconfirm VAG_TRW450_to_any.spec
if errorlevel 1 exit /b 1

echo.
echo Готово: dist\VAG_TRW450_to_any.exe
if "%~1"=="" pause
