# 稳序 WebApp · Python Agent + Go

本次融合实现见 [实现约定](docs/MERGE_IMPLEMENTATION.md)：PlanAgent 自主安排月度消费与投资钱包，方案展示金额、执行节奏及折叠原因；无模型不生成默认配置。MonthAgent 保留异常追问，账单可选；SummaryAgent 保留月度对比，并输出可视化就绪数据。

正式运行链路已整合为：浏览器 → Go（登录、账本、HTML 片段）→ 内部 HTTP/JSON → Python（五类 Agent、账单清洗、答疑与持久化）。前端采用 Go 模板、HTMX、daisyUI、ECharts，手机优先；不需要原生壳或前端 Node 服务。

**当前运行与部署以 [双服务操作说明](docs/WEB_OPERATIONS.md) 为准。** Windows 双击根目录 `start.cmd`，或执行 `./start.cmd`，即可同时启动 Python Agent 和 Go 网页服务，默认调用真实模型。请先停止此前单独启动的 Go 服务，启动后访问 http://127.0.0.1:8080。窗口需保持打开，按 Q / Esc / Ctrl+C 停止，直接关闭窗口也会一并停止两个服务；若仍有残留，双击根目录 `stop.cmd`（或执行 `./stop.cmd`）即可停止 Python 与 Go 并释放端口。启动失败会保留错误信息。首次使用需安装依赖并配置密钥（见操作说明）；离线测试执行 `./start.cmd -Offline`，自定义地址执行 `./start.cmd -Listen 127.0.0.1:8081`。数据使用新的 `data-web/go` 和 `data-web/agent`，旧数据目录保留。方案与复盘的算法、模型和数据结构以 Python 为准，旧 Go Agent 不再参与正式路由。

验收结果见 [Web 整合验收记录](docs/WEB_ACCEPTANCE_2026-10-08.md)。下文为原 Go MVP 的历史说明，单独运行 Go 不能替代 Python 服务，旧算法与旧单库备份方式不适用于完整应用。

---

面向中国大陆个人用户的轻量财务规划演示：账号隔离、CSV/手填账本、画像问答、可复算方案 Agent workflow、月末复盘。产品需求见 [PRD](docs/PRD.md)，算法和验收依据见 [WORKPLAN](WORKPLAN.md)，单线程界面路径见 [UX_FLOW](docs/UX_FLOW.md)。

## 本地运行

需要 Go 1.27+。在仓库根目录：

```powershell
go mod download
go run . -addr 127.0.0.1:8080 -data data
```

访问 `http://127.0.0.1:8080`。新账号先经 Agent 问答或手填并确认画像，然后进入账本；账本可载入模拟数据、下载 CSV 模板并预览上传，或直接手填交易。有记录后进入方案，复盘与答疑从方案页打开。可用两个账号验证数据隔离。默认从环境变量 `DEEPSEEK_API_KEY` 读取密钥；未设置时读取 `.env/deepseek_api.key`。无密钥时规则计算和复盘仍可用，智能解释降级为本地文本。密钥不应进入 Git。

## 算法边界

- 金额以分存储，账单以中国时区的日历月汇总；最近三个月中若有历史月份，排除尚未结束的本月。
- 50/30/20 是对照值，实际必要支出和债务优先。预备金默认为 3 或 6 个月必要支出；增长类上限依风险档为 0% / 20% / 40%。可在 `finance.go` 调整规则并更新算法版本和测试。
- `>3%` 是长期增长规划目标。界面中的收益率仅用固定假设情景比较，非市场预测或保证。新闻仅作背景，不改变风控计算。
- Agent 通过工具调用读取服务端计算结果；服务器在保存前独立校验金额、风险和来源。模型服务或资讯不可用时保留规则结果。

## VPS 构建与部署准备

本地或 CI 交叉编译 Linux amd64，VPS 只运行二进制和可选 HTTPS 反向代理：

```powershell
$env:GOOS = 'linux'
$env:GOARCH = 'amd64'
$env:CGO_ENABLED = '0'
go build -trimpath -ldflags '-s -w' -o bin/finance-linux-amd64 .
```

将二进制和密钥文件放到 VPS 的受限目录。示例 [systemd 单元](deploy/finance.service) 绑定本机 `127.0.0.1:8080`，示例 [Caddyfile](deploy/Caddyfile.example) 提供 HTTPS。替换示例域名和路径，再启动服务。反向代理方案参见 [Caddy 官方文档](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)。`COOKIE_SECURE=1` 用于 HTTPS 代理后标记会话 Cookie；直接本地 HTTP 开发不要设置。服务端始终从会话确定用户 ID。

当前容量配置在 `capacity.go`：最多 100 个账号、每账号 10,000 笔交易、每个对话范围保留最近 200 条消息、每账号保留最近 100 个方案。单次 CSV 不超过 2 MiB / 5000 行；并发生成最多 2 个、模型调用最多 2 个。根据目标 VPS 实测结果调整这些值。数据目录应留足 SQLite 主文件、WAL、临时文件和备份空间。线上目标机的资源压测仍需在获得 VPS 后执行。

## 备份与恢复

执行一致性备份，目标文件必须是尚不存在的新路径：

```sh
./finance-linux-amd64 -data /var/lib/finance -backup /var/backups/finance/finance-$(date +%F).db
```

备份成功后将快照移至异机存储。该命令使用 SQLite `VACUUM INTO` 生成一致性快照，避免直接复制正在写入的 WAL 数据库；原理见 [SQLite 文档](https://www.sqlite.org/lang_vacuum.html)。恢复时先停服务，再把验证过的快照作为 `finance.db` 放入数据目录，按该主机的操作规程处理原数据库文件，最后启动服务并检查 `/health` 和账号数据。不要把备份放入 Git。

## 验证

```powershell
go test ./...
go vet ./...
```

自动测试覆盖算法边界、CSV 解析与预览、CSRF、双账号完整流程和隔离、方案版本冲突、一致性备份。HTTP 页面及服务端链路已经过本地测试。浏览器可视化验收与目标 VPS 内存/磁盘压测需在可访问的环境中完成。
