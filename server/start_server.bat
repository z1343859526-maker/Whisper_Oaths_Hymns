@echo off
chcp 65001 >nul
title AI NPC Backend - golden_murder
cd /d "%~dp0"

echo =============================================
echo   黄金乡谋杀案 —— 后端启动器
echo   启动后请保持本窗口打开，按 Ctrl+C 停止
echo =============================================
echo.

REM 解释器定位：默认用 PATH 里的 python；也可用环境变量指定，如
REM   set PYTHON_EXE=D:\conda_envs\golden_murder\python.exe
if "%PYTHON_EXE%"=="" set "PYTHON_EXE=python"

"%PYTHON_EXE%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

echo.
echo =============================================
echo   后端已停止（正常退出或按了 Ctrl+C）
echo =============================================
pause
