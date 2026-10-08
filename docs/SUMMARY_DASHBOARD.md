# SummaryAgent 月度成果看板

已在现有 `/review?period=YYYY-MM` 页面接入 SummaryAgent 的真实结构化输出，没有新增模拟数据到业务路径。

## 展示内容

- 实际收入、支出、净结余、投资盈亏，以及同口径上月差额与环比。
- 单独计算“比上月少花 / 多花”，不混同于储蓄或投资回报。
- 本月储蓄计划完成度、实际与计划金额、累计已攒与总目标，以及 Agent 提供的成果依据。
- 结余去向：实际留存储蓄、新增投资本金，市场盈亏单列。
- 上月与本月对比图及可访问的金额明细表。
- 钱包执行进度：消费显示预算内与超支；储蓄和投资显示投入执行。
- 分类预算与实际支出、相邻月份分类变化。
- 可切换收支与结余 / 投资盈亏的历史趋势，以及复盘结论和下一月核对入口。

## 数据与交互

`integrated.go` 将 `summary_result.summary` 接入服务端看板和图表载荷。`review_dashboard.go` 读取 `insights`，兼容 `diff.insights`。页面主要金额与目标内容由 Go 模板渲染，图表使用现有本地 ECharts。图表与现有 HTMX 页面切换、ResizeObserver 和销毁逻辑一起工作。

资料未知不补零；真实零值照常显示；只有完整整月且 `comparable=true` 才计算环比及支出节省。上月金额为零或负数时不展示误导性的变化百分比。阶段回顾不展示最终达成徽章。历史图不把预计收入显示为实际收入，缺月断线，不展示所选月份之后的记录。

## 验证

- `go test ./...` 通过，包括新增金额口径、空值、零值、负结余、阶段限制、文本转义、完整页面与 HTMX 片段集成测试。
- `node scripts/test-review-charts.mjs` 通过：空值、零值、亏损、缺月、预计收入排除、未来记录排除、趋势切换。
- JavaScript 语法检查通过。
- 浏览器验收覆盖桌面、390 × 844 手机视口、投资盈亏与收支趋势切换；无控制台错误。
- 截图位于 `docs/acceptance/review-desktop.jpg`、`review-mobile.jpg`、`review-overview.jpg`。

验收截图及 `review-preview.html` 使用明确标注的合成演示数据，不代表真实账户结果。复现本地预览：

```powershell
$env:REVIEW_PREVIEW_DIR = 'docs/acceptance'
go test -run TestReviewDashboardRouteAndPreview -count=1
Remove-Item Env:REVIEW_PREVIEW_DIR
node scripts/preview-review.mjs
```

预览服务仅绑定 `127.0.0.1`，启动时打印系统分配的端口；不连接 Agent、不读真实账户。生产 Go 程序重新构建后会自动嵌入新增模板和静态资源。本次工作未部署线上。
