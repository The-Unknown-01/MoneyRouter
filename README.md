# 稳序 · MoneyRouter

面向中国大陆个人用户的月度财务规划 WebApp：先核对生活与资金事实，再生成可解释的钱包方案，并通过执行回顾积累后续规划经验。

**状态（2026-10-08）：核心功能、双服务 Web 整合、钱包 v2 和月份语义修正已实现；自动回归通过。月份修正后的完整业务验收曾中断，待续跑，尚未完成最新版真实模型、真机和目标 VPS 验收。**

## 当前架构

浏览器 → Go Web → 内部 HTTP/JSON → Python / LangGraph。

- Go：账号、会话、CSRF、账本、页面与 HTML 片段；个人请求身份由服务器会话确定。
- Python：五类 Agent、账单清洗、答疑、检查点、月度记录、方案版本与经验。
- 页面：Go 模板、HTMX、Tailwind/daisyUI、ECharts；手机优先，无全局侧栏。
- 模型：默认配置为 `deepseek-flash`；金融资料通过博查检索与 AKShare 数据层获取，记录来源、信息日期和缺口。
- 存储：Go SQLite 与 Python SQLite 检查点、业务文件分别保存。前端 Node 工具只用于构建。

当前契约见 [融合实现约定](docs/MERGE_IMPLEMENTATION.md)，运行和部署细节见 [双服务操作说明](docs/WEB_OPERATIONS.md)。仓库保留旧 Go 算法和历史方案兼容代码，正式业务以 Python Agent 为准。

## 使用流程

注册/登录 → 建立并确认画像 → 导入或补录 M 月完整账单 → 核对 M 月实际资料 → 确认 M 月整月复盘 → 补充并确认 M+1 月预计收入、费用与特殊安排 → 生成并确认 M+1 月钱包方案。

例如在十月默认核对九月账单，再制定十月方案。进入方案月份后记录实际执行；需要中途调整时，从当月核对进入独立的方案调整入口。具体状态与数据口径见 [月份主流程](docs/MONTH_WORKFLOW.md)。

画像是长期背景；月度资料单独核对。账单支持通用 CSV、支付平台 CSV/XLSX 和规范 JSON 的清洗预览、确认导入；也可直接在账本增删改流水。转账不计入收入与支出，未知字段保留未知。

PlanAgent 自主提出消费、缓冲、目标储蓄、额外还款和投资钱包。代码校验总额、已花与待支付义务、期限、风险和来源；通过后仍需用户显式确认。钱包是分配的唯一来源，旧固定比例、固定储备月数与经验自动缩放不用于正式方案。无模型时不能生成默认钱包配置，已保存资料和正式方案保留。

## 月份与金额口径

- 业务时区为 Asia/Shanghai，月份明确为 `YYYY-MM`；页面区分账本、核对、方案和复盘月份。
- 实际已到账收入与预计整月收入分别保存。预计整月收入包含已到账部分，不重复相加；实际结余只使用实际收入与实际支出。
- 当前月允许规划、执行调整与阶段回顾；主流程根据紧邻上一月的完整账单与已确认整月复盘制定方案。未来预案同样需要紧邻月份的完整依据，不能录入未来实际流水或复盘；历史月用于核对、查看已有方案和复盘。
- 用户确认不等于整月结账。最终复盘要求月份结束、资料完整且实际收入口径明确。
- 环比只比较紧邻上月；三月均值使用此前三个日历月，资料不足不补零。完整月份的明确零值与未知值分开处理。
- 复盘可选方案版本；后续规划单独核对 M+1 月预计资料，不复制 M 月事实。旧收入角色不明的记录需重新核对。

月份修正与验收状态见 [月份语义审计](docs/MONTH_SEMANTICS_AUDIT_2026-10-08.md)。

## 本地启动（Windows）

需要 Go 1.27+、Python 3.11+；重建静态资源需要 Node.js 与 pnpm。以下命令在本仓库根目录执行：

```powershell
python -m venv agent/.venv
./agent/.venv/Scripts/python.exe -m pip install -e './agent[api,test]'
# 真实金融数据层需要 AKShare；它带来 pandas 等较重依赖。
./agent/.venv/Scripts/python.exe -m pip install 'akshare>=1.19'
pnpm install --frozen-lockfile
pnpm build
```

真实模式配置 `DEEPSEEK_API_KEY`，或用 `DEEPSEEK_API_KEY_FILE` 指定密钥文件；默认文件为仓库 `.env/deepseek_api.key`。博查搜索同样支持 `BOCHA_API_KEY` / `BOCHA_API_KEY_FILE`，默认文件为 `.env/bocha_api.key`。密钥只在服务端读取。

```powershell
./start.cmd
# 离线功能检查，不验证真实模型，也不能生成默认钱包方案：
./start.cmd -Offline
# 自定义网页地址：
./start.cmd -Listen 127.0.0.1:8081
```

访问 [本地网页](http://127.0.0.1:8080)。启动脚本同时运行 Python（本机 8090）与 Go（默认 8080），自动配置内部令牌。运行窗口保持打开；按 Q / Esc / Ctrl+C 或关闭窗口停止两服务，残留可用 `./stop.cmd` 清理。不要同时启动旧 Go 服务占用同一端口。

数据位于 `data-web/go`、`data-web/agent`，市场缓存位于 `data-web/market-cache`；旧数据目录保留，不自动迁移。

## 验证与证据

```powershell
go test -count=1 ./...
go vet ./...
./agent/.venv/Scripts/python.exe -m pytest -q agent/tests
# 月份专项：
./agent/.venv/Scripts/python.exe -m pytest -q agent/tests/test_month_semantics.py
```

2026-10-08 状态梳理时全量结果：Python **531 passed**，Go test / vet 通过。本次文档更新前简单复核：Python 月份专项 **11 passed**，Go 月份相关测试通过。没有重新开展真实模型或浏览器完整验收。

- [Web 整合验收](docs/WEB_ACCEPTANCE_2026-10-08.md)：此前本机 HTTP、浏览器、启动停止和备份恢复证据。
- [钱包融合验收](docs/WALLET_MERGE_VALIDATION_2026-10-08.md)：钱包 v2 回归与离线联调证据。
- [旧版真实后端验收](docs/BACKEND_FULL_ACCEPTANCE_2026-10-07.md)：历史版本证据，不代表最新版钱包和月份行为已通过真实验收。

最新版验收仍需覆盖真实五 Agent 闭环、跨月场景、等待耗时与异常恢复、实体 Android/iPhone，以及目标 VPS 资源表现。下一步和完成标准见 [WORKPLAN](WORKPLAN.md)。

## 部署与备份

目标为日本 VPS：1 vCPU / 1 GiB RAM / 5 GiB 存储。部署 Go 二进制、Python 环境与两个 systemd 服务，Caddy 仅代理 Go；Python 8090 保持本机访问。配置参考 [agent.service](deploy/agent.service)、[web.service](deploy/web.service) 和 [Caddyfile](deploy/Caddyfile.example)。线上不运行 Node 构建服务。目标机尚未部署验收，AKShare/pandas 的内存与安装体积需实测。

完整应用备份须停止两服务，并同时备份 Go 与 Python 数据；单独运行旧 Go `-backup` 不构成完整备份。

```powershell
./stop.cmd
./scripts/backup-web.ps1 -DataRoot ./data-web -Destination ./backups/web-2026-10-08
```

目标目录必须不存在。自定义端口需另行确认服务已停止；恢复前核验清单 SHA256，在隔离目录检查账号、方案和检查点。详细步骤见 [操作说明](docs/WEB_OPERATIONS.md)。

建议限于预算和资产类别；收益目标与演示压力假设不构成预测或保证。现阶段不提供具体产品购买、代客交易或自动扣款。
