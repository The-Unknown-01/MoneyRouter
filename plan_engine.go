package main

import (
	"errors"
	"fmt"
	"math"
	"strings"
)

// These are disclosed stress-test assumptions for asset categories, not
// prescribed allocation percentages or forecasts of market returns.
const (
	steadyStressLossPct = 8.0
	growthStressLossPct = 35.0
)

type planProposal struct {
	ReserveTargetYuan float64 `json:"reserve_target_yuan" jsonschema_description:"Chosen liquid buffer target in yuan; zero only when no target is appropriate and justified"`
	ReserveKind       string  `json:"reserve_kind" jsonschema_description:"One of none, short_buffer, emergency"`
	ReserveReason     string  `json:"reserve_reason" jsonschema_description:"Reason based on the user's actual support, obligations and liquidity; no amounts or percentages"`
	ReservePct        int     `json:"reserve_pct" jsonschema_description:"Percent of this month's verified surplus going to the liquid buffer"`
	ExtraDebtPct      int     `json:"extra_debt_pct" jsonschema_description:"Percent of this month's verified surplus going to extra debt repayment"`
	GoalSavingsPct    int     `json:"goal_savings_pct" jsonschema_description:"Percent of this month's verified surplus going to a named near-term savings goal"`
	ConservativePct   int     `json:"conservative_pct" jsonschema_description:"Percent of this month's verified surplus going to accessible conservative savings"`
	SteadyPct         int     `json:"steady_pct" jsonschema_description:"Percent of this month's verified surplus going to a steady asset category"`
	GrowthPct         int     `json:"growth_pct" jsonschema_description:"Percent of this month's verified surplus going to a growth asset category"`
	RiskLabel         string  `json:"risk_label" jsonschema_description:"One of 低 中 高, describing the proposed allocation rather than imposing a fixed cap"`
	RiskReason        string  `json:"risk_reason" jsonschema_description:"Why this allocation fits the user's stated loss tolerance and time horizon; no amounts or percentages"`
	DecisionReason    string  `json:"decision_reason" jsonschema_description:"Concise personal trade-off rationale using the user's goals and features; no amounts or percentages"`
}

func preparePlan(p profile, c cashflow, sources []newsItem) plan {
	r := plan{Algorithm: algorithmVersion, Sources: sources, Warnings: []string{}, Trace: []traceStep{}}
	r.IncomeCents = p.IncomeCents
	if r.IncomeCents <= 0 {
		r.IncomeCents = c.IncomeCents
	}
	if !p.StableIncome && c.MinIncomeCents > 0 && c.MinIncomeCents < r.IncomeCents {
		r.IncomeCents = c.MinIncomeCents
		r.Warnings = append(r.Warnings, "收入波动较大，预算采用账单中的较低月收入")
	}
	r.NeedsCents = c.NeedsCents
	r.DebtCents = max64(p.DebtCents, c.DebtCents)
	r.WantsCents = c.WantsCents
	switch c.Source {
	case "unknown":
		r.Provisional = true
		r.DataQuality = "尚无月支出估计，仅显示临时收支摘要"
	case "conversation":
		r.DataQuality = "基于对话中确认的月总支出估计，开销类别尚未明确"
	case "conversation_categories":
		r.DataQuality = "基于对话记录的分类月开销；未分类部分按必要开支保守计入"
	case "quick":
		r.DataQuality = "基于已核对的分类月开销；未分类部分按必要开支保守计入"
	default:
		if c.Months < 2 {
			r.DataQuality = "账单样本不足两个月，结果仅作初步参考"
		} else {
			r.DataQuality = fmt.Sprintf("基于最近 %d 个月的账单汇总", c.Months)
		}
	}
	r.Trace = append(r.Trace, traceStep{Tool: "verified_cashflow", Detail: fmt.Sprintf("月收入 %s；必要开支 %s；可选开支 %s；最低还款 %s", formatMoney(r.IncomeCents), formatMoney(r.NeedsCents), formatMoney(r.WantsCents), formatMoney(r.DebtCents)), Version: algorithmVersion})
	if r.Provisional {
		r.Narrative = "月支出尚不明确，无法可靠计算结余与资金分配。可在画像对话中补充粗略月支出；分类账本仍可跳过。"
		return r
	}
	if p.OutcomeKnown && (c.Source == "quick" || c.Source == "conversation_categories") && c.NeedsCents+c.WantsCents+c.DebtCents > p.OutcomeCents {
		r.Warnings = append(r.Warnings, "分类开销合计高于此前的月总支出估计，分析采用较高的分类数据")
	}
	committed := r.NeedsCents + r.DebtCents + r.WantsCents
	r.SavingsCents = max64(0, r.IncomeCents-committed)
	if committed > r.IncomeCents {
		r.Warnings = append(r.Warnings, "已知开支和最低还款超过收入；本月没有可分配结余，请优先核对收支")
	}
	r.Trace = append(r.Trace, traceStep{Tool: "budget_reference", Detail: fmt.Sprintf("50/30/20 仅作参考：%s / %s / %s；实际开支和最低还款优先；可分配结余 %s", formatMoney(r.IncomeCents/2), formatMoney(r.IncomeCents*30/100), formatMoney(r.IncomeCents*20/100), formatMoney(r.SavingsCents)), Version: algorithmVersion})
	return r
}

func validAgentReason(reason string) bool {
	return reason != "" && len(reason) <= 600 && !strings.ContainsAny(reason, "0123456789%％¥￥")
}

func applyPlanProposal(base plan, p profile, candidate planProposal) (plan, error) {
	r := base
	if r.Provisional {
		return plan{}, errors.New("月支出未知，不能提交资金分配")
	}
	if math.IsNaN(candidate.ReserveTargetYuan) || math.IsInf(candidate.ReserveTargetYuan, 0) || candidate.ReserveTargetYuan < 0 || candidate.ReserveTargetYuan > 1_000_000_000 {
		return plan{}, errors.New("流动资金目标金额无效")
	}
	if candidate.ReserveKind != "none" && candidate.ReserveKind != "short_buffer" && candidate.ReserveKind != "emergency" {
		return plan{}, errors.New("流动资金目标类型无效")
	}
	if !validAgentReason(candidate.ReserveReason) || !validAgentReason(candidate.RiskReason) || !validAgentReason(candidate.DecisionReason) {
		return plan{}, errors.New("理由应简短、基于用户事实且不自行书写金额或比例")
	}
	if candidate.RiskLabel != "低" && candidate.RiskLabel != "中" && candidate.RiskLabel != "高" {
		return plan{}, errors.New("风险标签无效")
	}
	percentages := []int{candidate.ReservePct, candidate.ExtraDebtPct, candidate.GoalSavingsPct, candidate.ConservativePct, candidate.SteadyPct, candidate.GrowthPct}
	totalPct := 0
	for _, pct := range percentages {
		if pct < 0 || pct > 100 {
			return plan{}, errors.New("结余分配比例必须在零到一百之间")
		}
		totalPct += pct
	}
	if (r.SavingsCents > 0 && totalPct != 100) || (r.SavingsCents == 0 && totalPct != 0) {
		return plan{}, errors.New("各项结余分配比例之和必须与可分配结余一致")
	}
	r.ReserveKind = candidate.ReserveKind
	r.ReserveReason = candidate.ReserveReason
	r.ReserveTarget = int64(math.Round(candidate.ReserveTargetYuan * 100))
	r.ReserveGap = max64(0, r.ReserveTarget-p.ReserveCents)
	r.ReserveMonthly = r.SavingsCents * int64(candidate.ReservePct) / 100
	r.DebtExtra = r.SavingsCents * int64(candidate.ExtraDebtPct) / 100
	r.GoalSavings = r.SavingsCents * int64(candidate.GoalSavingsPct) / 100
	r.Steady = r.SavingsCents * int64(candidate.SteadyPct) / 100
	r.Growth = r.SavingsCents * int64(candidate.GrowthPct) / 100
	r.Conservative = r.SavingsCents - r.ReserveMonthly - r.DebtExtra - r.GoalSavings - r.Steady - r.Growth
	r.InvestableCents = r.Conservative + r.Steady + r.Growth
	r.Risk = candidate.RiskLabel
	r.RiskReason = candidate.RiskReason
	r.DecisionReason = candidate.DecisionReason
	if candidate.ReserveKind == "none" && r.ReserveTarget != 0 {
		return plan{}, errors.New("无缓冲目标时目标金额应为零")
	}
	if candidate.ReserveKind != "none" && r.ReserveTarget <= 0 {
		return plan{}, errors.New("设置缓冲目标时需给出正金额")
	}
	if r.ReserveMonthly > r.ReserveGap {
		return plan{}, errors.New("本月留存超过流动资金缺口，请将剩余金额安排到其他类别")
	}
	if r.DebtExtra > 0 && r.DebtCents == 0 {
		return plan{}, errors.New("没有已知债务，不能安排额外还款")
	}
	if r.Growth > 0 && p.HorizonMonths < 24 {
		return plan{}, errors.New("增长类资金需要较长使用期限；当前期限不足，请降低增长配置")
	}
	if r.Steady > 0 && p.HorizonMonths < 12 {
		return plan{}, errors.New("近期需要使用的资金应保持流动性，请降低稳健配置")
	}
	if r.InvestableCents > 0 {
		r.StressLossPct = math.Round((float64(r.Steady)*steadyStressLossPct+float64(r.Growth)*growthStressLossPct)/float64(r.InvestableCents)*100) / 100
		if r.StressLossPct > float64(p.MaxLossPct)+0.001 {
			return plan{}, fmt.Errorf("情景压力损失 %.2f%% 超过用户已确认的可承受亏损 %d%%；请降低波动资产", r.StressLossPct, p.MaxLossPct)
		}
		weighted := float64(r.Conservative)*2 + float64(r.Steady)*3.5 + float64(r.Growth)*6
		r.ScenarioPct = math.Round((weighted/float64(r.InvestableCents)-0.5)*100) / 100
		r.GoalAchievable = r.ScenarioPct > 3
	}
	r.Trace = append(r.Trace,
		traceStep{Tool: "agent_proposal", Detail: fmt.Sprintf("Agent 自主提出结余分配：缓冲 %d%%、额外还款 %d%%、目标储蓄 %d%%、保守 %d%%、稳健 %d%%、增长 %d%%", candidate.ReservePct, candidate.ExtraDebtPct, candidate.GoalSavingsPct, candidate.ConservativePct, candidate.SteadyPct, candidate.GrowthPct), Version: algorithmVersion},
		traceStep{Tool: "stress_check", Detail: fmt.Sprintf("按稳健下跌 %.0f%%、增长下跌 %.0f%% 的演示假设，配置情景损失 %.2f%%；用户已确认可承受 %d%%", steadyStressLossPct, growthStressLossPct, r.StressLossPct, p.MaxLossPct), Version: algorithmVersion},
	)
	if err := validateAgentPlan(r); err != nil {
		return plan{}, err
	}
	return r, nil
}

func validateAgentPlan(p plan) error {
	if p.IncomeCents < 0 || p.NeedsCents < 0 || p.DebtCents < 0 || p.WantsCents < 0 || p.SavingsCents < 0 || p.ReserveMonthly < 0 || p.DebtExtra < 0 || p.GoalSavings < 0 || p.Conservative < 0 || p.Steady < 0 || p.Growth < 0 {
		return errors.New("方案金额不得为负")
	}
	if p.Provisional {
		return nil
	}
	if p.SavingsCents != max64(0, p.IncomeCents-p.NeedsCents-p.DebtCents-p.WantsCents) {
		return errors.New("结余与核实的收支不一致")
	}
	if p.ReserveMonthly+p.DebtExtra+p.GoalSavings+p.Conservative+p.Steady+p.Growth != p.SavingsCents {
		return errors.New("结余分配金额不守恒")
	}
	if p.Conservative+p.Steady+p.Growth != p.InvestableCents || p.ReserveMonthly > p.ReserveGap {
		return errors.New("配置或流动资金金额不一致")
	}
	for _, source := range p.Sources {
		if source.URL == "" || source.Title == "" {
			return errors.New("引用缺少出处")
		}
	}
	return nil
}
