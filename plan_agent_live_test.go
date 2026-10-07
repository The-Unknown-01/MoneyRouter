package main

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"
)

// Run explicitly with FINANCE_LIVE_AGENT_TEST=1 to verify the installed
// DeepSeek adapter and tool loop. Ordinary tests stay offline and repeatable.
func TestLivePersonalPlanAgent(t *testing.T) {
	if os.Getenv("FINANCE_LIVE_AGENT_TEST") != "1" {
		t.Skip("live model test is opt-in")
	}
	client := newDeepseekClient()
	if !client.Available() {
		t.Skip("DeepSeek key is not configured")
	}
	profile := profile{
		IncomeCents: 250_000, OutcomeCents: 200_000, OutcomeKnown: true,
		Feature:      "大学生，家庭承担学费和房租，每月生活费稳定到账，没有债务，旅行目标近期需要用钱",
		IncomeSource: "家庭生活费", StableIncome: true, HorizonMonths: 12,
		MaxLossPct: 0, Goal: "储蓄旅行费用", Experience: "none",
	}
	cash := cashflow{Source: "conversation_categories", NeedsCents: 160_000, WantsCents: 40_000, Months: 1}
	base := preparePlan(profile, cash, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 75*time.Second)
	defer cancel()
	result, err := client.GeneratePersonalPlan(ctx, profile, cash, base)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateAgentPlan(result); err != nil {
		t.Fatal(err)
	}
	if result.Growth != 0 || result.Steady != 0 {
		t.Fatalf("zero loss tolerance was not respected: %+v", result)
	}
	t.Logf("kind=%s target=%s buffer=%s goal=%s conservative=%s trace=%d", result.ReserveKind, formatMoney(result.ReserveTarget), formatMoney(result.ReserveMonthly), formatMoney(result.GoalSavings), formatMoney(result.Conservative), len(result.Trace))
}

func TestLiveProfileCategoryCapture(t *testing.T) {
	if os.Getenv("FINANCE_LIVE_AGENT_TEST") != "1" {
		t.Skip("live model test is opt-in")
	}
	a := testApp(t)
	a.deepseek = newDeepseekClient()
	if !a.deepseek.Available() {
		t.Skip("DeepSeek key is not configured")
	}
	_, _, userID := registerTestUser(t, a, "live_category")
	p, err := a.getProfile(userID)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 55*time.Second)
	defer cancel()
	reply, err := a.profileAgentReply(ctx, userID, &p, nil, "我是大学生，每月家里给我2500元生活费，房租和学费由家里另付。我一个月总花2000元，其中餐饮900元，交通200元，想攒旅行费。")
	if err != nil {
		t.Fatal(err)
	}
	var count int
	if err := a.db.QueryRow(`SELECT count(*) FROM expense_estimates WHERE user_id=?`, userID).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if p.IncomeCents != 250_000 || !p.OutcomeKnown || p.OutcomeCents != 200_000 || count < 2 {
		t.Fatalf("profile/category extraction incomplete: income=%d outcome=%d known=%t categories=%d reply=%q", p.IncomeCents, p.OutcomeCents, p.OutcomeKnown, count, reply)
	}
	if strings.Contains(reply, "怎么分") || strings.Contains(reply, "给你出") {
		t.Fatalf("profile crossed planning boundary: %q", reply)
	}
	t.Logf("categories=%d handoff=%q", count, reply)
}
