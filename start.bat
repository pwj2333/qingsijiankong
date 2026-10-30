@echo off
setlocal
cd /d %~dp0

if not exist config.json (
  echo [ERROR] 未找到 config.json，请先复制 config.example.json 为 config.json
  exit /b 1
)

if not exist credentials.json (
  echo [ERROR] 未找到 credentials.json，请复制 credentials.example.json 并填写平台和邮箱信息
  exit /b 1
)

set PYTHONUTF8=1
set PYTHONPATH=%~dp0src

python run.py --config config.json --credentials credentials.json %*

endlocal
