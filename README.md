# 勤思报警分类处理与邮件通知系统

Python 3.10+ 常驻任务，支持 Linux、Windows 和 Docker。通过勤思平台接口查询未处理报警，按配置分类处理并发送邮件。

## 处理规则

| 报警类型 | 分流 | 平台操作 | 邮件 |
|---|---|---|---|
| 包含“人员离岗”“未定期巡检”“未定期巡查” | 每条独立 50% 抽中 | 有效；处理方式“通知已处理” | 平台处理成功后发送 |
| 上述重点类型 | 未抽中 | 保持未处理 | 不发送 |
| 其他非空类型 | 全部 | 无效；处理方式“识别错误” | 不发送 |

“未定期巡查机舱”等包含关键词的类型也适用。关键词与比例可配置。抽样结果按报警 ID 保存，重启或修改规则不会重新抽取已经记录的报警。50% 是逐条概率，不保证每天正好一半。

仅处理**新版本第一次正常启动时刻及以后发生**的报警，精度为秒；此前报警保持原状。首次上线分界和查询进度保存在 SQLite，重启不重置。请保留整个 `data` 目录。缺少 ID、类型或有效发生时间的报警不提交平台。

抽样是自动处理策略，不能判断现场真伪。系统和邮件注明“系统按配置自动处理”，不记录未经核实的整改结果。**平台文档未明确列出“通知已处理”选项，上线前必须确认 `handleMethod="通知已处理"` 能被接口接受，且平台页面显示正确。**

## 填写配置

Linux/macOS：

```bash
cp config.example.json config.json
cp credentials.example.json credentials.json
```

Windows PowerShell：

```powershell
Copy-Item config.example.json config.json
Copy-Item credentials.example.json credentials.json
```

在 `credentials.json` 填写平台账号、密码和 SMTP 信息：

```json
{
  "platform": {
    "account": "填写勤思平台账号",
    "password": "填写勤思平台密码"
  },
  "email": {
    "smtp_host": "填写SMTP服务器",
    "smtp_port": 465,
    "tls_mode": "ssl",
    "username": "填写发件邮箱账号",
    "authorization_code": "填写SMTP授权码或专用密码",
    "sender": "sender@example.com",
    "recipients": ["recipient@example.com"]
  }
}
```

示例占位值必须替换。所有船舶统一发送给 `recipients` 中的每个邮箱，分别投递，不公开其他收件人。支持 `ssl`（通常 465）和 `starttls`（通常 587），验证 TLS 证书。SMTP 授权码按邮箱服务商要求开通。

`config.json` 中的业务参数：

| 参数 | 默认值 / 用途 |
|---|---|
| `poll_interval_seconds` | 30，平台轮询周期 |
| `request_timeout_seconds` / `email_timeout_seconds` | 20，接口 / SMTP 超时 |
| `lookback_hours` | 24，每次查询窗口上限；停机补查会分窗口覆盖完整间隔 |
| `page_size` / `order_by` | 50 / 0，查询分页和排序 |
| `sampling.keywords` | 三个重点类型关键词，按包含关系匹配 |
| `sampling.probability` | 0.5，范围 0～1 |
| `auto_handle.valid_handle_method` | 通知已处理 |
| `auto_handle.invalid_handle_method` | 识别错误 |
| `auto_handle.handle_result` / `remark` | 明确说明系统按配置自动处理，供负责人核实 |
| `auto_handle.is_archive` | 0，不归档 |
| `email_retry_minutes` | `[1, 5, 15, 30]`，失败后逐次等待，之后重复最后一个间隔 |
| `state_db_path` | `data/alarm_state.sqlite3`，业务状态和邮件任务 |
| `processed_store_path` | `data/processed_ids.json`，仅用于首次导入旧版已处理 ID |
| `log_dir` / `excel_log_path` | 文本日志目录 / Excel 明细 |

配置中的相对数据、日志路径以 `config.json` 所在目录为基准。修改配置后重启生效；已分流任务沿用保存的决定、处理参数和收件人。凭据文件不进入 Git 或镜像，日志不输出密码、授权码和 Token。

## 本地运行

```bash
python -m pip install -r requirements.txt
python run.py --config config.json --credentials credentials.json --test-email
python run.py --config config.json --credentials credentials.json
```

`--test-email` 仅发送测试邮件，不访问平台、不建立业务状态库；所有收件人均被 SMTP 接受才返回 0。`--once` 执行一轮真实平台处理以及当前到期的邮件任务后退出，不等待未来重试；仍有平台或邮件任务待重试时返回 1。常驻运行才会持续重试。

```bash
python run.py --config config.json --credentials credentials.json --once
```

Windows 也可运行 `start.bat`，或 `start.bat --test-email`。配置错误返回 2，执行失败返回 1。全程按北京时间计算查询范围和日志时间，与服务器默认时区无关。

## Linux Docker 部署

填写两个配置文件后，在项目目录构建并运行当前代码：

```bash
mkdir -p data logs
docker compose build
docker compose run --rm qingsijiankong python run.py --test-email
docker compose up -d --no-build
docker compose logs -f --tail=100
```

Compose 将配置和凭据只读挂载，将 `data`、`logs` 持久化挂载。未创建两个配置文件时不会自动把它们建成目录。无需对外开放端口，服务器需要能访问勤思 HTTPS 接口和邮箱 SMTP 服务。

默认构建本地 `qingsijiankong:local`，避免与远程旧版 `latest` 混用。GitHub Actions 在 Linux、Windows 测试通过后构建并发布镜像；已发布新版本时，可改用：

```bash
export QINGSI_IMAGE=ghcr.io/pwj2333/qingsijiankong:latest
docker compose pull
docker compose up -d --no-build
```

服务器无法访问 Docker Hub 时可使用已发布镜像；修改后的本地代码需要构建或发布后才会进入镜像。

## 恢复与运维

- 每轮先完整读取所有分页并保存，再提交处理，避免待处理列表缩短导致翻页跳过。保存已成功查询的时间进度，默认重叠查询 60 秒以覆盖分页边界和短时延迟。
- SQLite 保存首次上线时间、每个 ID 的抽样结果、平台提交状态和每位收件人的邮件任务。平台处理与创建邮件任务使用持久化状态衔接；提交超时或崩溃时，查询全状态报警核实已有结果后才决定重试。
- 超时后查到与任务一致的处理结果，补建邮件任务；仍未处理则重试；已被其他方式处理则保留平台结果。查不到或状态不明时保持“结果待核实”，等待后续核对，不盲目重提。
- 平台成功、邮件失败时不回退平台状态。邮件后台独立重试，已成功的收件人不重复投递。日志区分“平台已处理”“邮件待发送”“邮件发送失败”“SMTP 已接受”；SMTP 接受不代表最终送达或用户已阅读。
- SMTP 无法与本地数据库组成事务；若服务器已接受但响应丢失，或接受后立即断电，重试可能重复投递。同一任务复用 Message-ID，方便邮件系统识别和追查。
- 仅支持一个账号、一个运行实例；进程锁阻止同一状态库同时启动两个实例。不同账号使用独立数据目录。重试邮件即使对应报警已不在未处理列表，也会继续发送。
- 首次启动自动导入旧版 `processed_ids.json`，原文件不修改。损坏时明确报错，不静默丢失历史记录。升级前停旧进程并备份整个数据目录；回退旧版会恢复其“全部标无效”行为，应先停服务再评估。
- 备份 SQLite 时停止容器后复制整个 `data` 目录；运行中的 WAL 文件属于数据库的一部分，不应只复制主文件。Excel 为辅助明细，业务恢复以 SQLite 为准。

## 验证

```bash
python -m unittest discover -s tests -v
```

测试使用模拟平台和 SMTP，不访问真实账号、不发送实际邮件。覆盖分类、固定抽样、历史保护、分页、停机补查、平台超时核对、旧 ID 导入、邮件独立重试、TLS、凭据校验和北京时间。
