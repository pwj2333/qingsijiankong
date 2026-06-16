# 勤思异常报警自动处理系统

这是一个 Python 常驻任务，用来轮询勤思平台的未处理告警，并按固定规则自动提交处理结果。

## 本地运行

1. 安装 Python 3.10+
2. 安装依赖
```bash
pip install -r requirements.txt
```
3. 复制配置文件
```bash
copy config.example.json config.json
```
4. 编辑 `config.json`，填写账号和密码
5. 启动
```bash
python run.py --config config.json
```

只跑一轮：
```bash
python run.py --config config.json --once
```

## Docker

镜像启动后默认读取 `/app/config.json`，日志和状态文件分别写入 `/app/logs` 和 `/app/data`。

构建：
```bash
docker build -t qingsijiankong:latest .
```

运行：
```bash
docker run -d \
  --name qingsijiankong \
  --restart unless-stopped \
  -v "$(pwd)/config.json:/app/config.json" \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/logs:/app/logs" \
  qingsijiankong:latest
```

## GitHub Actions

仓库推送到 GitHub 后，会自动构建并推送镜像到 GitHub Container Registry：

`ghcr.io/pwj2333/qingsijiankong:latest`

## Linux 部署

```bash
git clone https://github.com/pwj2333/qingsijiankong.git
cd qingsijiankong
cp config.example.json config.json
mkdir -p data logs
vi config.json
docker login ghcr.io
docker pull ghcr.io/pwj2333/qingsijiankong:latest
docker run -d \
  --name qingsijiankong \
  --restart unless-stopped \
  -v "$(pwd)/config.json:/app/config.json" \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/logs:/app/logs" \
  ghcr.io/pwj2333/qingsijiankong:latest
```

如果你更喜欢 Compose：
```bash
docker compose up -d
```
