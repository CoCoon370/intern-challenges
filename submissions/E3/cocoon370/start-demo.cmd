@echo off
chcp 65001 >nul
setlocal

set "CANDIDATE_DIR=%~dp0"
for %%I in ("%CANDIDATE_DIR%\..\..\..") do set "REPO_DIR=%%~fI"
set "PYTHON=%REPO_DIR%\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
  echo 未找到项目虚拟环境：%PYTHON%
  echo 请先按照 README.md 的“首次准备”安装依赖。
  pause
  exit /b 1
)

cd /d "%REPO_DIR%"
echo 正在启动约半分钟的可视化 Demo，请观察随后打开的 Edge 窗口……
"%PYTHON%" "%CANDIDATE_DIR%run_local_eval.py" --headed

echo.
echo 测试结束。详细结果保存在：
echo %CANDIDATE_DIR%local-out\result.json
echo %CANDIDATE_DIR%local-out\decisions.jsonl
pause

