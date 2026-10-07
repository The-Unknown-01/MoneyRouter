package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strconv"
	"strings"
)

const algorithmVersion = "budget-workflow-v1"

type profile struct {
	IncomeCents   int64
	OutcomeCents  int64
	OutcomeKnown  bool
	Feature       string
	IncomeSource  string
	StableIncome  bool
	FamilyLoad    bool
	DebtCents     int64
	ReserveCents  int64
	HorizonMonths int
	MaxLossPct    int
	Experience    string
	Goal          string
	Confirmed     bool
}

type cashflow struct {
	Source         string `json:"source"`
	Months         int    `json:"months"`
	IncomeCents    int64  `json:"income_cents"`
	MinIncomeCents int64  `json:"min_income_cents"`
	NeedsCents     int64  `json:"needs_cents"`
	WantsCents     int64  `json:"wants_cents"`
	DebtCents      int64  `json:"debt_cents"`
	Count          int    `json:"count"`
}

type traceStep struct {
	Tool    string `json:"tool"`
	Detail  string `json:"detail"`
	Version string `json:"version"`
}

type plan struct {
	Provisional     bool        `json:"provisional"`
	Version         int         `json:"version"`
	Algorithm       string      `json:"algorithm"`
	IncomeCents     int64       `json:"income_cents"`
	NeedsCents      int64       `json:"needs_cents"`
	DebtCents       int64       `json:"debt_cents"`
	WantsCents      int64       `json:"wants_cents"`
	SavingsCents    int64       `json:"savings_cents"`
	ReserveMonths   int         `json:"reserve_months"`
	ReserveTarget   int64       `json:"reserve_target"`
	ReserveGap      int64       `json:"reserve_gap"`
	ReserveMonthly  int64       `json:"reserve_monthly"`
	InvestableCents int64       `json:"investable_cents"`
	Conservative    int64       `json:"conservative"`
	Steady          int64       `json:"steady"`
	Growth          int64       `json:"growth"`
	Risk            string      `json:"risk"`
	RiskReason      string      `json:"risk_reason"`
	GrowthCapPct    int         `json:"growth_cap_pct"`
	ScenarioPct     float64     `json:"scenario_pct"`
	GoalAchievable  bool        `json:"goal_achievable"`
	DataQuality     string      `json:"data_quality"`
	Warnings        []string    `json:"warnings"`
	Sources         []newsItem  `json:"sources"`
	Trace           []traceStep `json:"trace"`
	Narrative       string      `json:"narrative"`
}

func formatMoney(cents int64) string {
	negative := ""
	if cents < 0 {
		negative = "−"
		cents = -cents
	}
	return fmt.Sprintf("%s¥%d.%02d", negative, cents/100, cents%100)
}

func parseMoney(raw string) (int64, error) {
	raw = strings.TrimSpace(strings.ReplaceAll(strings.ReplaceAll(raw, ",", ""), "¥", ""))
	if raw == "" {
		return 0, errors.New("请输入金额")
	}
	if strings.HasPrefix(raw, "-") {
		return 0, errors.New("金额不能为负数")
	}
	parts := strings.Split(raw, ".")
	if len(parts) > 2 || len(parts[0]) == 0 {
		return 0, errors.New("金额格式无效")
	}
	whole, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil || whole > 1_000_000_000 {
		return 0, errors.New("金额超出范围")
	}
	var fraction int64
	if len(parts) == 2 {
		if len(parts[1]) < 1 || len(parts[1]) > 2 {
			return 0, errors.New("金额最多两位小数")
		}
		frac := parts[1]
		if len(frac) == 1 {
			frac += "0"
		}
		fraction, err = strconv.ParseInt(frac, 10, 64)
		if err != nil {
			return 0, errors.New("金额格式无效")
		}
	}
	return whole*100 + fraction, nil
}

func max64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}
func min64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}

func computePlan(p profile, c cashflow, sources []newsItem) plan {
	result := plan{Algorithm: algorithmVersion, Sources: sources, Warnings: []string{}, Trace: []traceStep{}}
	step := func(tool, detail string) {
		result.Trace = append(result.Trace, traceStep{Tool: tool, Detail: detail, Version: algorithmVersion})
	}
	result.IncomeCents = p.IncomeCents
	if result.IncomeCents <= 0 {
		result.IncomeCents = c.IncomeCents
	}
	if !p.StableIncome && c.MinIncomeCents > 0 && c.MinIncomeCents < result.IncomeCents {
		result.IncomeCents = c.MinIncomeCents
		result.Warnings = append(result.Warnings, "收入波动较大，预算采用账单中的较低月收入")
	}
	result.NeedsCents = c.NeedsCents
	result.DebtCents = max64(p.DebtCents, c.DebtCents)
	if c.Source == "unknown" {
		result.Provisional = true
		result.DataQuality = "尚无月支出估计，仅提供临时建议"
	} else if c.Source == "conversation" {
		result.DataQuality = "基于对话中确认的月支出估计；可随时补充分类开销"
	} else if c.Source == "quick" {
		result.DataQuality = "基于手填的分类月开销估计"
	} else if c.Months < 2 {
		result.DataQuality = "账单样本不足两个月，结果仅作初步参考"
	} else {
		result.DataQuality = fmt.Sprintf("基于最近 %d 个月的账单汇总", c.Months)
	}
	step("summarize_cashflow", fmt.Sprintf("月收入 %s；必要开支 %s；可选开支 %s；债务还款 %s；样本 %d 个月", formatMoney(result.IncomeCents), formatMoney(c.NeedsCents), formatMoney(c.WantsCents), formatMoney(result.DebtCents), c.Months))
	if result.IncomeCents <= 0 {
		result.Warnings = append(result.Warnings, "缺少有效收入，请在画像中补充税后月收入")
	}
	if c.Source == "conversation" && p.DebtCents > p.OutcomeCents {
		result.Warnings = append(result.Warnings, "月债务还款高于所述总支出，请核对 Outcome；本方案优先计入债务")
	}
	if result.Provisional {
		result.Warnings = append(result.Warnings, "尚不知道每月大致支出，无法安全计算结余和具体投资金额")
		result.Risk, result.RiskReason, result.GrowthCapPct = assessRisk(p, result.IncomeCents)
		result.Narrative = "先记录收入与个人特点；补充一个大致的月支出数字后，才能计算预备金与资金分配。账本可继续跳过。"
		step("await_outcome", "缺少月支出估计，暂停具体资金分配")
		return result
	}
	available := result.IncomeCents - result.NeedsCents - result.DebtCents
	if available < 0 {
		result.Warnings = append(result.Warnings, "必要支出与债务还款已超过收入；当前不安排增长配置")
		available = 0
	}
	result.WantsCents = min64(c.WantsCents, min64(available, result.IncomeCents*30/100))
	result.SavingsCents = max64(0, available-result.WantsCents)
	step("allocate_budget", fmt.Sprintf("50/30/20 对照：必要 %s、可选 %s、储蓄 %s；本方案先覆盖实际必要开支和债务，再安排可选支出 %s 与结余 %s", formatMoney(result.IncomeCents/2), formatMoney(result.IncomeCents*30/100), formatMoney(result.IncomeCents*20/100), formatMoney(result.WantsCents), formatMoney(result.SavingsCents)))
	result.ReserveMonths = 3
	if !p.StableIncome || p.FamilyLoad {
		result.ReserveMonths = 6
	}
	result.ReserveTarget = result.NeedsCents * int64(result.ReserveMonths)
	result.ReserveGap = max64(0, result.ReserveTarget-p.ReserveCents)
	result.ReserveMonthly = min64(result.SavingsCents, result.ReserveGap)
	result.InvestableCents = max64(0, result.SavingsCents-result.ReserveMonthly)
	step("calculate_reserve", fmt.Sprintf("%d 个月 × %s = %s；现有 %s；缺口 %s；本月补足 %s", result.ReserveMonths, formatMoney(result.NeedsCents), formatMoney(result.ReserveTarget), formatMoney(p.ReserveCents), formatMoney(result.ReserveGap), formatMoney(result.ReserveMonthly)))
	result.Risk, result.RiskReason, result.GrowthCapPct = assessRisk(p, result.IncomeCents)
	step("assess_risk", fmt.Sprintf("%s；增长类上限 %d%%；%s", result.Risk, result.GrowthCapPct, result.RiskReason))
	if result.DebtCents > 0 && result.DebtCents*100 > result.IncomeCents*30 {
		result.Warnings = append(result.Warnings, "债务还款占收入较高，请优先核对债务成本")
	}
	if result.InvestableCents > 0 {
		switch result.Risk {
		case "高":
			result.Growth = result.InvestableCents * 40 / 100
			result.Steady = result.InvestableCents * 40 / 100
		case "中":
			result.Growth = result.InvestableCents * 20 / 100
			result.Steady = result.InvestableCents * 50 / 100
		default:
			result.Steady = 0
		}
		result.Conservative = result.InvestableCents - result.Growth - result.Steady
	}
	step("propose_allocation", fmt.Sprintf("可配置 %s：保守 %s、稳健 %s、增长 %s", formatMoney(result.InvestableCents), formatMoney(result.Conservative), formatMoney(result.Steady), formatMoney(result.Growth)))
	if len(sources) > 0 {
		step("find_sources", fmt.Sprintf("检索到 %d 条带原文链接的资讯，仅用于解释宏观背景", len(sources)))
	} else {
		step("find_sources", "外部资讯暂不可用，方案不依赖新闻")
	}
	if result.InvestableCents > 0 {
		// These rates are scenarios, not forecasts: 2%, 3.5%, 6%, less 0.5% costs.
		weighted := float64(result.Conservative)*2.0 + float64(result.Steady)*3.5 + float64(result.Growth)*6.0
		result.ScenarioPct = math.Round((weighted/float64(result.InvestableCents)-0.5)*100) / 100
		result.GoalAchievable = result.ScenarioPct > 3
	}
	step("check_goal", fmt.Sprintf("长期规划目标 >3%%；示例情景扣费后 %.2f%%，仅为假设演算，不代表预测", result.ScenarioPct))
	if result.InvestableCents == 0 {
		result.Warnings = append(result.Warnings, "当前月度结余优先用于预备金或保障开支，暂不新增投资")
	}
	if !result.GoalAchievable {
		result.Warnings = append(result.Warnings, "在当前示例情景下未达到 >3% 目标；不会为凑目标增加风险")
	}
	step("validate_plan", "校验金额合计、预备金、风险上限与来源；用户确认后保存版本")
	return result
}

func assessRisk(p profile, income int64) (string, string, int) {
	willing := 0
	if p.MaxLossPct >= 15 {
		willing = 2
	} else if p.MaxLossPct >= 5 {
		willing = 1
	}
	horizon := 0
	if p.HorizonMonths >= 60 {
		horizon = 2
	} else if p.HorizonMonths >= 24 {
		horizon = 1
	}
	capacity := 2
	if !p.StableIncome || p.Experience == "none" {
		capacity = 1
	}
	if income <= 0 || (p.DebtCents > 0 && p.DebtCents*100 > income*30) {
		capacity = 0
	}
	level := willing
	if horizon < level {
		level = horizon
	}
	if capacity < level {
		level = capacity
	}
	labels := []string{"低", "中", "高"}
	caps := []int{0, 20, 40}
	reason := fmt.Sprintf("亏损意愿 %d%%、投资期限 %d 个月、收入与债务约束取较保守档", p.MaxLossPct, p.HorizonMonths)
	return labels[level], reason, caps[level]
}

func validatePlan(p plan) error {
	if p.IncomeCents < 0 || p.NeedsCents < 0 || p.DebtCents < 0 || p.WantsCents < 0 || p.SavingsCents < 0 || p.InvestableCents < 0 {
		return errors.New("金额不得为负")
	}
	if p.NeedsCents+p.DebtCents+p.WantsCents+p.SavingsCents > p.IncomeCents && p.IncomeCents >= p.NeedsCents+p.DebtCents {
		return errors.New("预算超过收入")
	}
	if p.Conservative+p.Steady+p.Growth != p.InvestableCents {
		return errors.New("配置金额不平衡")
	}
	if p.InvestableCents > 0 && p.Growth*100 > p.InvestableCents*int64(p.GrowthCapPct) {
		return errors.New("增长配置超过风险上限")
	}
	if p.ReserveMonthly+p.InvestableCents != p.SavingsCents {
		return errors.New("结余分配不平衡")
	}
	for _, s := range p.Sources {
		if s.URL == "" || s.Title == "" {
			return errors.New("引用缺少出处")
		}
	}
	return nil
}

func (p plan) JSON() string { b, _ := json.Marshal(p); return string(b) }
