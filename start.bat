@echo off
setlocal
cd /d %~dp0

if not exist config.json (
  echo [ERROR] 未找到 config.json，请先复制 config.example.json 为 config.json 并填写账号密码
  exit /b 1
)

set PYTHONUTF8=1
set PYTHONPATH=%~dp0src

python run.py --config config.json

endlocal
