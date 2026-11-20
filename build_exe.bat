@echo off
setlocal
cd /d "%~dp0"

python -m pip install --upgrade flet
if errorlevel 1 exit /b 1

flet pack abs_editor.py -y -n VAG_TRW450_to_any -i assets\app.ico --product-name "VAG TRW450 to any" --file-description "Редактор таблицы констант ABS TRW 450" --add-data "HELP.md:." --add-data "assets:assets"
if errorlevel 1 exit /b 1

echo.
echo Готово: dist\VAG_TRW450_to_any.exe
if "%~1"=="" pause
