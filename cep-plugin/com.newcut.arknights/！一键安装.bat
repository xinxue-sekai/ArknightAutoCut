@echo off
chcp 65001 >nul
rem ============================================================
rem  NewCut 面板一键安装（仅需运行一次）
rem
rem  做两件事：
rem   1. 放行未签名 CEP 面板（写入当前用户注册表，无需管理员）
rem   2. 自检插件文件完整性
rem
rem  之后：重启 Premiere Pro → 菜单「窗口 - 扩展 - NewCut 明日方舟自动剪辑」
rem ============================================================

echo [1/2] 放行未签名 CEP 面板（PlayerDebugMode）...
reg add "HKCU\Software\Adobe\CSXS.9"  /v PlayerDebugMode /t REG_SZ /d 1 /f >nul 2>&1
reg add "HKCU\Software\Adobe\CSXS.10" /v PlayerDebugMode /t REG_SZ /d 1 /f >nul 2>&1
reg add "HKCU\Software\Adobe\CSXS.11" /v PlayerDebugMode /t REG_SZ /d 1 /f >nul 2>&1
reg add "HKCU\Software\Adobe\CSXS.12" /v PlayerDebugMode /t REG_SZ /d 1 /f >nul 2>&1
echo       完成。

echo [2/2] 自检插件文件...
set "BASE=%~dp0"
if not exist "%BASE%manifest.xml"      (echo       缺少 manifest.xml & goto :fail)
if not exist "%BASE%index.html"        (echo       缺少 index.html & goto :fail)
if not exist "%BASE%jsx\host.jsx"      (echo       缺少 jsx\host.jsx & goto :fail)
if not exist "%BASE%runtime\pythonw.exe" (echo       缺少 runtime（嵌入式 Python） & goto :fail)
if not exist "%BASE%engine\server.py"  (echo       缺少 engine & goto :fail)
if not exist "%BASE%engine\template_pack\manifest.json" (
    echo       提示: 没有模板包，分析前需先校准（见 README）
)
echo       插件文件完整。

echo.
echo 全部完成！请完全退出并重新打开 Premiere Pro，
echo 然后在菜单「窗口 - 扩展」里找到「NewCut 明日方舟自动剪辑」。
pause
exit /b 0

:fail
echo.
echo 插件文件不完整：请复制整个 com.newcut.arknights 文件夹（不要只复制部分内容）。
pause
exit /b 1
