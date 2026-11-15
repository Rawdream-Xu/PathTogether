@echo off
chcp 65001 >nul
title PathTogether 本地平台
echo ============================================
echo   PathTogether 一键启动（WSL + PG + 平台）
echo ============================================
wsl.exe -d Ubuntu-22.04 -- bash -lc 'bash "/mnt/f/切片三维重建软件平台构建/本地部署/pt-app-ctl.sh" start'
if errorlevel 1 (
  echo.
  echo [启动失败] 请查看 WSL 内 ~/pt-data/logs/error.log
  pause
  exit /b 1
)
echo.
echo 正在打开浏览器 http://localhost:8000 ...
start http://localhost:8000
echo 完成。本窗口可关闭；停止平台请运行「PathTogether停止.bat」
timeout /t 5 >nul
