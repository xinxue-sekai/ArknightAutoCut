@echo off
rem NewCut 分析服务 —— 取消开机自启
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v NewCutService /f
echo [NewCut] 已取消开机自启。后台服务若在运行，可用任务管理器结束 pythonw.exe。
pause
