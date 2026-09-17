@echo off
chcp 65001 >nul
title start-demo

REM ============================================================
REM  一键 demo 启动器（Windows）
REM    1) 准备三仓共享的 AUTH_SECRET（fail-closed 之下没密钥服务起不来）
REM    2) 串行启动三个服务：p1 orchestrator :8010 / p2 customer :8001 / rag :8002
REM  关闭某个窗口 = 停掉那个服务。
REM
REM  前提：三个仓库克隆在同一个父目录下（customer-service-agent / rag-agent 与
REM        orchestrator-agent 同级），且已按 README 装好依赖。
REM ============================================================

set HERE=%~dp0
set ROOT=%HERE%..

REM 优先用仓库内虚拟环境；没有就回落到 PATH 里的 python
set P1PY=%HERE%.venv/Scripts/python.exe
if not exist "%P1PY%" set P1PY=python
set P2PY=%ROOT%/customer-service-agent/.venv/Scripts/python.exe
if not exist "%P2PY%" set P2PY=python
set RAGPY=%ROOT%/rag-agent/.venv/Scripts/python.exe
if not exist "%RAGPY%" set RAGPY=python

echo ============================================================
echo   [1/2] 准备共享密钥 AUTH_SECRET
echo ============================================================
"%P1PY%" "%HERE%scripts/demo_bootstrap.py"
if errorlevel 1 (
  echo.
  echo   X 密钥准备失败，服务未启动。
  echo     先按上面提示对齐三仓密钥，再重新运行本脚本。
  pause
  exit /b 1
)

echo.
echo ============================================================
echo   [2/2] 启动服务   p1:8010   p2:8001   rag:8002
echo ============================================================
echo.

start "p1-orchestrator-8010" /D "%ROOT%/orchestrator-agent" cmd /k "%P1PY% -m uvicorn main:app --host 127.0.0.1 --port 8010"
timeout /t 2 >nul

start "p2-customer-8001" /D "%ROOT%/customer-service-agent" cmd /k "%P2PY% -m uvicorn main:app --host 127.0.0.1 --port 8001"
timeout /t 2 >nul

start "rag-qa-8002" /D "%ROOT%/rag-agent" cmd /k "%RAGPY% -m uvicorn main:app --host 127.0.0.1 --port 8002"

echo.
echo 三个窗口已启动（rag 要加载嵌入模型，首次约 1-2 分钟）。
echo 健康检查：
echo   http://127.0.0.1:8010/health
echo   http://127.0.0.1:8001/health
echo   http://127.0.0.1:8002/health
echo.
echo 演示入口（浏览器打开）：http://127.0.0.1:8010/   观测台 http://127.0.0.1:8010/ops
echo 演示账号：customer/customer123   agent/agent123   admin/admin123
echo.
pause
