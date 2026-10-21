@echo off
REM Windows：雙擊這個檔案就會啟動逐句錄音，瀏覽器自動打開
cd /d "%~dp0"
python server.py
pause
