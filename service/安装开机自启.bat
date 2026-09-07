@echo off
rem NewCut 分析服务 —— 开机自启安装（当前用户，无需管理员）
rem 安装后每次登录 Windows 自动在后台启动服务（无窗口），
rem 面板在 PR 里即可直接使用，无需再手动运行任何脚本。
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [NewCut] 首次安装：正在安装 Python 依赖（uv sync）...
    call uv sync
    if errorlevel 1 (
        echo [NewCut] 依赖安装失败，请检查 uv 是否安装：pip install uv
        pause & exit /b 1
    )
)

set "PYW=%~dp0.venv\Scripts\pythonw.exe"
set "SRV=%~dp0newcut_service\server.py"

reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v NewCutService /d "\"%PYW%\" \"%SRV%\" --port 8765" /f
if errorlevel 1 (
    echo [NewCut] 注册表写入失败
    pause & exit /b 1
)

echo [NewCut] 已注册开机自启（HKCU\...\Run\NewCutService）
echo [NewCut] 正在立即启动一次服务...
start "" "%PYW%" "%SRV%" --port 8765
timeout /t 2 >nul
curl -s http://127.0.0.1:8765/status
echo.
echo [NewCut] 完成。打开 PR 面板，顶部圆点变绿即可使用。
pause
