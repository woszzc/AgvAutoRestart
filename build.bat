@echo off
chcp 65001 >nul
setlocal

echo ==========================================
echo  智能应用定时重启 - EXE 打包
echo ==========================================

if not exist .venv (
    echo [1/4] 创建虚拟环境...
    py -3 -m venv .venv
)

echo [2/4] 安装依赖...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt

echo [3/4] 开始 PyInstaller 打包...
pyinstaller --noconfirm --clean --windowed --onefile --name AgvAutoRestart --icon=app.ico --add-data "app.ico;." main.py

echo [4/4] 打包完成
echo.
echo EXE 文件：
echo dist\AgvAutoRestart.exe
echo.
pause
