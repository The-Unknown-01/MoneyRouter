# 财务规划 WebApp（工行杯 MVP）

面向中国大陆个人用户的轻量财务规划演示：账号隔离、CSV/手填账本、画像问答、可复算方案 Agent workflow、月末复盘。产品需求见 [PRD](docs/PRD.md)，算法和验收依据见 [WORKPLAN](WORKPLAN.md)。

## 本地运行

需要 Go 1.27+。在仓库根目录：

```powershell
go mod download
go run . -addr 127.0.0.1:8080 -data data
```

访问 `http://127.0.0.1:8080`。可创建两个账号，分别点击“载入模拟数据”，在方案页生成版本，再到上个月复盘页验证数据隔离。也可在账本下载 CSV 模板，预览并确认上传；或直接手填交易。默认从环境变量 `DEEPSEEK_API_KEY` 读取密钥；未设置时读取 `.env/deepseek_api.key`。无密钥时规则计算和复盘仍可用，智能解释降级为本地文本。密钥不应进入 Git。

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
