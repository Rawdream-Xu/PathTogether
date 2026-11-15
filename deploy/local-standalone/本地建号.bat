@echo off
chcp 65001 >nul
title PathTogether 本地建号
setlocal
set "LOGIN_ID=%~1"
if "%LOGIN_ID%"=="" (
  set /p "LOGIN_ID=请输入新账号登录名（字母/数字/邮箱样式）: "
)
if "%LOGIN_ID%"=="" (
  echo [取消] 未输入登录名。
  timeout /t 3 >nul
  exit /b 1
)
echo.
echo 密码 15..200 字符（输入无回显，需输入两次确认）……
wsl.exe -d Ubuntu-22.04 -- bash -lc '"$HOME/pt-venv/bin/python" "/mnt/f/切片三维重建软件平台构建/本地部署/create_local_user.py" "$1"' _ "%LOGIN_ID%"
if errorlevel 1 (
  echo.
  echo [建号失败] 请检查上方错误信息；平台未启动时请先运行「PathTogether一键启动.bat」
  pause
  exit /b 1
)
echo.
echo 完成。请用新账号在 http://localhost:8000/login 登录。
timeout /t 5 >nul
