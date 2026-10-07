package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
)

const algorithmVersion = "agent-allocation-v2"

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
	ReserveKind     string      `json:"reserve_kind"`
	ReserveReason   string      `json:"reserve_reason"`
	DebtExtra       int64       `json:"debt_extra"`
	GoalSavings     int64       `json:"goal_savings"`
	InvestableCents int64       `json:"investable_cents"`
	Conservative    int64       `json:"conservative"`
	Steady          int64       `json:"steady"`
	Growth          int64       `json:"growth"`
	Risk            string      `json:"risk"`
	RiskReason      string      `json:"risk_reason"`
	GrowthCapPct    int         `json:"growth_cap_pct,omitempty"` // Retained only to read earlier saved plan versions.
	StressLossPct   float64     `json:"stress_loss_pct"`
	DecisionReason  string      `json:"decision_reason"`
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

func (p plan) JSON() string { b, _ := json.Marshal(p); return string(b) }
