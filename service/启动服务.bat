@echo off
rem NewCut 本地分析服务启动脚本（面板依赖此服务）
cd /d "%~dp0"
uv run python -m newcut_service.server --port 8765
pause
