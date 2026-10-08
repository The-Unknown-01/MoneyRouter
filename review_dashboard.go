package main

import (
	"fmt"
	"strconv"
)

type reviewMetric struct {
	Label, Value, Change, Tone, Note string
}
type reviewProgress struct {
	Name, Actual, Planned, Status, Tone string
	Percent                             int64
	Known                               bool
}
type reviewDashboard struct {
	CategoryChanges                                                                                            []reviewMetric
	Metrics                                                                                                    []reviewMetric
	Wallets                                                                                                    []reviewProgress
	Comparison                                                                                                 []reviewMetric
	Milestones                                                                                                 []string
	Goal, GoalStatus, GoalActual, GoalPlanned, GoalTotal, GoalTarget, GoalPercent, AsOf, Previous, PlanVersion string
	GoalBar                                                                                                    int64
	GoalKnown, Final, Comparable                                                                               bool
	Retained, Invested, MarketReturn, Surplus                                                                  string
}

func reviewPercent(actual, target any) (string, int64, bool) {
	if actual == nil || target == nil || num(target) <= 0 {
		return "待补充", 0, false
	}
	pct := float64(num(actual)) / float64(num(target)) * 100
	bar := int64(pct)
	if bar < 0 {
		bar = 0
	}
	if bar > 100 {
		bar = 100
	}
	return fmt.Sprintf("%.1f%%", pct), bar, true
}

func signedReviewMoney(v int64) string {
	if v > 0 {
		return "+" + formatMoney(v)
	}
	return formatMoney(v)
}

// Read only SummaryAgent evidence. Missing values remain unknown, never zero.
func buildReviewDashboard(summary map[string]any) *reviewDashboard {
	i := obj(summary["insights"])
	if len(i) == 0 {
		i = obj(obj(summary["diff"])["insights"])
	}
	g, accounting := obj(i["goal"]), obj(i["accounting"])
	d := &reviewDashboard{Final: str(summary["review_mode"]) == "final", Comparable: yes(i["comparable"]), AsOf: str(i["as_of"]), Previous: str(i["previous_period"]), PlanVersion: str(obj(summary["diff"])["plan_version"])}
	d.Comparable = d.Final && d.Comparable
	d.Goal = str(g["goal"])
	if d.Goal == "" {
		d.Goal = "为你的目标积累"
	}
	d.GoalActual, d.GoalPlanned = displayCents(g["saved_this_month_cents"]), displayCents(g["planned_this_month_cents"])
	d.GoalTotal, d.GoalTarget = displayCents(g["saved_to_date_cents"]), displayCents(g["target_cents"])
	d.GoalPercent, d.GoalBar, d.GoalKnown = reviewPercent(g["saved_this_month_cents"], g["planned_this_month_cents"])
	d.GoalStatus = "待补充目标数据"
	if d.GoalKnown {
		d.GoalStatus = "阶段进度 · 月末再看达成"
		if d.Final {
			d.GoalStatus = "尚未达到本月计划"
			if num(g["saved_this_month_cents"]) >= num(g["planned_this_month_cents"]) {
				d.GoalStatus = "本月储蓄目标已达成"
			}
		}
	}
	d.Retained, d.Invested = displayCents(accounting["retained_cents"]), displayCents(accounting["invested_cents"])
	d.MarketReturn, d.Surplus = displayCents(accounting["market_return_cents"]), displayCents(accounting["surplus_cents"])
	metrics := map[string]map[string]any{}
	labels := map[string]string{"income_cents": "实际收入", "spend_total_cents": "实际支出", "balance_cents": "实际净结余", "investment_return_cents": "投资盈亏"}
	for _, raw := range arr(i["metrics"]) {
		m := obj(raw)
		metrics[str(m["id"])] = m
	}
	for _, id := range []string{"income_cents", "spend_total_cents", "balance_cents", "investment_return_cents"} {
		m := metrics[id]
		card := reviewMetric{Label: labels[id], Value: displayCents(m["current_cents"]), Change: "上月资料不足，暂不比较", Tone: "neutral"}
		if !d.Final {
			card.Change = "阶段数据，暂不计算整月环比"
		}
		if d.Comparable && m["delta_cents"] != nil {
			delta := num(m["delta_cents"])
			card.Change = "较上月 " + signedReviewMoney(delta)
			if m["delta_pct"] != nil && m["previous_cents"] != nil && num(m["previous_cents"]) > 0 {
				card.Change += fmt.Sprintf("（%+.1f%%）", floatValue(m["delta_pct"]))
			}
		}
		card.Note = map[string]string{"income_cents": "仅计已到账收入", "spend_total_cents": "本期核对的消费支出", "balance_cents": "收入 − 支出", "investment_return_cents": "市场盈亏，独立于收支结余"}[id]
		if id == "investment_return_cents" && m["current_cents"] != nil && num(m["current_cents"]) < 0 {
			card.Tone = "caution"
		}
		d.Comparison = append(d.Comparison, reviewMetric{Label: labels[id], Value: displayCents(m["current_cents"]), Change: displayCents(m["previous_cents"]), Note: card.Change})
		d.Metrics = append(d.Metrics, card)
	}
	saving := reviewMetric{Label: "比上月少花", Value: "暂不可比较", Change: "需要相邻两个月的完整资料", Tone: "neutral", Note: "支出差额，不等于储蓄或投资收益"}
	if d.Comparable && metrics["spend_total_cents"]["delta_cents"] != nil {
		delta := num(metrics["spend_total_cents"]["delta_cents"])
		if delta <= 0 {
			saving.Value = formatMoney(-delta)
			saving.Change = "同口径支出较上月减少"
			saving.Tone = "positive"
		} else {
			saving.Label = "比上月多花"
			saving.Value = formatMoney(delta)
			saving.Change = "结合本月安排理解支出变化"
			saving.Tone = "caution"
		}
	}
	d.Metrics = append(d.Metrics, saving)
	if d.Comparable {
		for _, raw := range arr(i["category_changes"]) {
			c := obj(raw)
			if c["delta_cents"] == nil {
				continue
			}
			d.CategoryChanges = append(d.CategoryChanges, reviewMetric{Label: str(c["id"]), Value: signedReviewMoney(num(c["delta_cents"])), Change: "上月 " + displayCents(c["previous_cents"]) + " → 本月 " + displayCents(c["current_cents"])})
		}
	}
	for _, raw := range arr(i["wallet_execution"]) {
		w := obj(raw)
		_, bar, known := reviewPercent(w["actual_cents"], w["planned_cents"])
		row := reviewProgress{Name: str(w["name"]), Actual: displayCents(w["actual_cents"]), Planned: displayCents(w["planned_cents"]), Percent: bar, Known: known, Tone: "neutral", Status: "执行待核对"}
		if w["actual_cents"] != nil && w["planned_cents"] != nil {
			if str(w["kind"]) == "expense" {
				row.Status, row.Tone = "预算内", "positive"
				if num(w["actual_cents"]) > num(w["planned_cents"]) {
					row.Status, row.Tone = "超出 "+formatMoney(num(w["actual_cents"])-num(w["planned_cents"])), "caution"
				}
			} else {
				row.Status = "已记录投入"
				if d.Final && num(w["planned_cents"]) > 0 && num(w["actual_cents"]) >= num(w["planned_cents"]) {
					row.Status, row.Tone = "计划投入已完成", "positive"
				}
			}
		}
		d.Wallets = append(d.Wallets, row)
	}
	if d.Final {
		for _, raw := range arr(i["milestones"]) {
			if label := str(obj(raw)["label"]); label != "" {
				d.Milestones = append(d.Milestones, label)
			}
		}
	}
	return d
}

func floatValue(v any) float64 {
	n, _ := strconv.ParseFloat(str(v), 64)
	return n
}
