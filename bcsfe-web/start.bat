@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 필요한 프로그램을 설치하는 중이에요 (처음에는 1분 정도 걸려요)...
py -m pip install -q -r requirements.txt || python -m pip install -q -r requirements.txt
echo.
echo 세이브 에디터를 시작해요: http://localhost:8000
start "" http://localhost:8000
py app.py || python app.py
pause
