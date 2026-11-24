@echo off
rem 安装依赖，然后做本机设置（选麦克风、找微信、挂 Claude 钩子、开机自启、启动）。
chcp 65001 >nul
cd /d "%~dp0"
py -3 -m venv .venv || (echo 没找到 Python 3.12 或 3.13，请先从 python.org 安装（勾选 Add to PATH）。 & pause & exit /b 1)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt || (pause & exit /b 1)
echo.
.venv\Scripts\python voice_input.py --setup
pause
