@echo off
chcp 65001 >nul
title PathTogether 停止
echo 正在停止 PathTogether（应用 + PostgreSQL）...
wsl.exe -d Ubuntu-22.04 -- bash -lc 'bash "/mnt/f/切片三维重建软件平台构建/本地部署/pt-app-ctl.sh" stop'
echo 已停止。
timeout /t 3 >nul
