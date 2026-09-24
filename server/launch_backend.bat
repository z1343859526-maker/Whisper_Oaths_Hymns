@echo off
chcp 65001 >nul
title AI NPC Backend - golden_murder (auto)

echo =============================================
echo   黄金乡谋杀案 —— AI NPC 后端（由客户端自动拉起）
echo   关闭本窗口或退出游戏即可停止后端
echo =============================================
echo.

cd /d "%~dp0"

REM 解释器定位：默认用 PATH 里的 python；也可用环境变量指定，如
REM   set PYTHON_EXE=D:\conda_envs\golden_murder\python.exe
if "%PYTHON_EXE%"=="" set "PYTHON_EXE=python"

"%PYTHON_EXE%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
