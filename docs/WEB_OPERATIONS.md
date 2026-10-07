# 双服务运行、契约与备份

Go 持有账号、会话、账本；Python 持有画像、检查点、月度记录、方案版本、经验和画像事件。浏览器只能访问 Go。`agent/src/moneyrouter_agent/api/service.py` 为 `/v1` 内部服务实现，带令牌的 `/v1/schema` 和 `/openapi.json` 是契约依据，版本为 `1`。未知字段为 null，已知零为 0；领域金额为整数分，规范账单文件为元的十进制字符串。转账不计入收支。

## 本地运行

需要 Go（版本见 go.mod）、Python 3.11+、Node 与 pnpm。仓库根目录执行：

```powershell
python -m venv agent/.venv
./agent/.venv/Scripts/python.exe -m pip install -e './agent[api,test]'
pnpm install --frozen-lockfile
pnpm build
./scripts/start-web.ps1 -Offline
```

访问 http://127.0.0.1:8080。停止 Go 后启动脚本会停止其 Python 子进程。真实模型联调去掉 `-Offline`，配置 Python 支持的 `DEEPSEEK_API_KEY` 或密钥文件。已有旧 `data` 不改动，新数据在 `data-web/go` 与 `data-web/agent`；线上不需要 Node 进程。静态包已锁版本，本地托管且嵌入 Go 二进制；修改前端后须重新构建 Go。

独立启动时，两个进程设置相同的随机 `AGENT_SERVICE_TOKEN`。Python 设置 `PYTHONPATH=agent/src`、`MONEYROUTER_SERVICE_DIR`，运行 `python -m uvicorn moneyrouter_agent.api.server:app --host 127.0.0.1 --port 8090`。Go 的 `AGENT_SERVICE_URL` 默认为 http://127.0.0.1:8090。使用 `-data data-web/go` 运行 Go；不要将 Python 端口公开。设置 `MARKET_DATA_CACHE_DIR` 可将市场缓存放在数据目录。

任务提交返回 202/job_id，状态为 queued/running/succeeded/failed。相同 request_id 的不同内容拒绝提交。每账号同一时间一个任务，全局两个 worker。重启后未完成任务标为中断；查看当前结果后重试，已落定会话从 SQLite 检查点恢复，确认和版本写入去重。所有确认都要求显式 action=confirm；离开页面不会取消后台任务。

## 部署准备

`deploy/agent.service` 安装为 `moneyrouter-agent.service`；`deploy/web.service` 安装为 `moneyrouter-web.service`。配置 `/etc/moneyrouter.env`（权限 600）中的服务令牌与模型配置，创建 finance 用户，部署目录 `/opt/moneyrouter`，数据目录 `/var/lib/finance/go` 和 `/var/lib/finance/agent`。Caddy 只代理 Go 8080，HTTPS 配置 `COOKIE_SECURE=1`。

启动前构建本地静态包与 Linux Go 二进制，安装 Python `[api]` 依赖。先检查带令牌的 Python `/readyz`，再检查 Go `/readyz`。本次没有执行远程部署。

## 一致性备份与恢复

必须同时停止两个服务，再复制整个 Go 和 Python 数据目录，包括 SQLite、WAL/SHM、用户检查点和业务 JSON 文件。只运行 Go 的 `-backup` 不构成完整应用备份。令牌和模型配置另外安全保管，备份不纳入版本库。

本地停止启动脚本后运行：

```powershell
./scripts/backup-web.ps1 -DataRoot ./data-web -Destination ./backups/web-2026-10-08
```

脚本拒绝覆盖现有目标，并拒绝在本机 8080/8090 仍监听时备份。自定义端口或远程环境需自行确认两个进程均已停止。Linux 对应步骤：`systemctl stop moneyrouter-web moneyrouter-agent`，复制 `/var/lib/finance` 到新目录，计算校验摘要，再启动两项服务。

恢复时停止两服务，将当前目录改名保留，核验备份 `manifest.json` 的 SHA256 后把完整 `data` 内容复制到新的 `data-web`（Linux 为 `/var/lib/finance`）。不要混合不同时间的 Go 数据库与 Python 检查点。恢复权限，启动 Python 和 Go，检查 readyz、账号隔离、待确认会话、方案版本及月度记录。先在隔离目录验证恢复，再替换正式目录。
