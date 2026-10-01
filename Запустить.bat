@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Запуск «Смывы» на http://localhost:8000 ...
echo Не закрывайте это окно, пока работаете. Для остановки закройте окно.
start "" http://localhost:8000/input
python app_server.py
pause
