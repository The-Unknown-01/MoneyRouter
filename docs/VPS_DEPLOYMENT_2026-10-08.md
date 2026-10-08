# VPS 部署记录（2026-10-08，Asia/Shanghai）

已将远程 `main` 提交 `f4aca91291bade93d637f1b02262bff9e7bef66b` 部署至 `103.193.149.104`。

访问入口：https://anota.best 。域名 DNS 已确认指向 `103.193.149.104`；HTTP 自动跳转至域名 HTTPS（域名入口 308，原 IP 入口 301）。Caddy 已获得 Let's Encrypt 证书并负责自动续期，Go 已启用 `COOKIE_SECURE=1`。

## 部署布局

- 系统：Debian 11，x86_64；实际内存约 475 MiB，磁盘约 3 GiB，无 swap。
- 应用源码及 Go 程序：`/opt/moneyrouter`，版本标记：`/opt/moneyrouter/DEPLOYED_COMMIT`。
- Python：独立 CPython 3.12.15，运行时位于 `/opt/moneyrouter-tools/python`；虚拟环境位于 `/opt/moneyrouter/agent/.venv`。
- uv 0.12.23、Caddy 2.11.7：官方发行包，经官方校验和核验；工具位于 `/opt/moneyrouter-tools`。
- Go 和 Python 以 `finance` 用户运行；数据位于 `/var/lib/finance/go`、`/var/lib/finance/agent`，市场缓存位于 `/var/lib/finance/market-cache`。
- 配置位于 `/etc/moneyrouter.env`，权限 `600 root:root`；两个应用密钥位于 `/etc/moneyrouter`，权限 `640 root:finance`，传输获用户明确授权。
- `moneyrouter-agent` 监听 `127.0.0.1:8090`；`moneyrouter-web` 监听 `127.0.0.1:8080`；`moneyrouter-proxy` 使用 Caddy 在公网 443 端口代理 Go，80 端口用于 HTTPS 跳转和证书验证。
- Caddy 证书持久化于 systemd StateDirectory `/var/lib/moneyrouter-caddy`；存储配置位于 `/etc/systemd/system/moneyrouter-proxy.service.d/storage.conf`。
- 三个 systemd 服务均已启用开机启动。服务器原 APT 源未改动。

## 验证结果

- 从隔离的远程 main 源码构建 Linux Go 程序；本地与远端二进制 SHA256 一致：`40bdc9ce62a0c59eb0d95f317a7d1fd7a760a07d95753c65af4b8c9b7f02d2d1`。
- 同一源码的 Go 全量测试通过；Python 全量测试 `531 passed in 33.67s`（本机已有依赖环境）。
- 外网 HTTPS `/login` 返回 200，HTTPS `/readyz` 返回 `ok`，未跳过证书验证；HTTP `/login` 返回 308，目标为 `https://anota.best/login`。
- 本机代理与 Go 的 `/healthz`、`/readyz`、`/login`、`/register`、`/static/ui.css` 全部返回 200。
- Python 内部接口无令牌返回 401，携带令牌的 readiness 返回 contract_version `1`。
- 实际 DeepSeek 画像任务成功，非 degraded；验收用内部用户数据已通过清理接口删除。
- 博查真实检索 HTTP 200、API code 200，返回结果；AKShare 1.19.1 安装并成功导入。
- 重启 Go 和 Python 后 readiness 恢复正常；Python 启动约需 5 秒，期间代理 readiness 会短暂返回 503。
- 最终空闲运行时约使用 176 MiB 系统内存，可用内存约 286 MiB，磁盘余量约 1.6 GiB。此为冒烟检查期间观测，未做并发负载测试。

本次完成部署与冒烟检查；不代表五 Agent 完整业务闭环、所有金融数据源、手机实机或跨月场景全部验收。

## 运维

```sh
systemctl status moneyrouter-agent moneyrouter-web moneyrouter-proxy
journalctl -u moneyrouter-agent -u moneyrouter-web -u moneyrouter-proxy -f
systemctl restart moneyrouter-agent moneyrouter-web
curl -fsS http://127.0.0.1/readyz
```

部署日志：`/root/moneyrouter-deploy.log`；冒烟日志：`/root/moneyrouter-verify.log`。

备份前同时停止 Go 与 Python，完整备份 `/var/lib/finance`，另行保护 `/etc/moneyrouter.env` 和 `/etc/moneyrouter`；详细流程见 `WEB_OPERATIONS.md`。

## 更名与图标更新部署（2026-10-08，同日第二次部署）

将部署从 `24350f55…` 更新为 `d5e3675a0dff2e0f8970b8ce225bf6fa6dbf50fb`：应用正式更名为「薪安理得」，应用图标改用 `icon.png`。

- 构建：以 `git -c core.autocrlf=false archive` 取出的 LF 内容为源，在 Windows 侧 `CGO_ENABLED=0 GOOS=linux GOARCH=amd64` 构建；部署后 `/opt/moneyrouter/bin/finance-linux-amd64` SHA256 为 `bdb509867b6e85c8d93ba7a94f1a5316f40416d4e99323af182cb84eec7bb339`，与本地构建一致。
- 上传内容：新 Go 二进制、变更源文件 tar（模板与静态资源内嵌进二进制；Python `agent/src` 源码与 venv 内已安装副本同步更新，`PYTHONPATH` 指向 `/opt/moneyrouter/agent/src`），另刷新两个 unit 的 `Description`。
- 备份：`/root/moneyrouter-backup-20261008-045338`（旧二进制 `e25ed191…`、旧 `DEPLOYED_COMMIT`、完整源码 tar、新旧二进制 SHA256）。
- 部署与验证脚本：`/root/deploy-d5e3675/{deploy,verify}-vps-d5e3675.sh`；日志 `/root/moneyrouter-deploy.log`、`/root/moneyrouter-verify-d5e3675.log`。
- 验证结果：三个服务 `active`；本机 8080 的 `/healthz`、`/readyz`、`/login`、`/register`、`/static/{ui.css,flow.css,icon.png}` 全部 200；`/login` 标题为「登录 · 薪安理得」且页面无旧名；`flow.css` 含 `img.brand-mark` 规则；内部接口无令牌 401、带令牌 `/readyz` 返回 `contract_version: 1`；Agent 人设与 FastAPI title 已为「薪安理得」；公网 `https://anota.best/login` 200、`/static/icon.png` 200（1,285,896 字节，SHA256 `4d7719dc…` 与仓库 `web/static/icon.png` 一致）、HTTP 入口仍为 308。抽查 9 个源码文件，远端与本地 SHA256 完全一致。
- 回滚：`install -m 0755 /root/moneyrouter-backup-20261008-045338/finance-linux-amd64.24350f5 /opt/moneyrouter/bin/finance-linux-amd64 && tar xzf /root/moneyrouter-backup-20261008-045338/source.tar.gz -C /opt/moneyrouter && systemctl restart moneyrouter-agent moneyrouter-web`。
- 未改动：Caddy 配置、`/etc/moneyrouter.env`、数据目录与数据库结构；本次无数据迁移，故未做停机备份。
- 待确认：连接时发现 SSH 主机密钥与 `known_hosts` 中原有 ECDSA 条目不一致，新主机密钥为 ED25519 `SHA256:3mJaKpEuUZsApw7JGUFjtCi2rcljfthCY+zkUKQtsvw`。已按部署指纹（`DEPLOYED_COMMIT` = `24350f5`、`/opt/moneyrouter` 布局、`anota.best`）确认为同一实例，但建议核对主机密钥变更原因。
