package main

import (
	"strings"
	"testing"
)

func testProposal() planProposal {
	return planProposal{
		ReserveKind: "none", ReserveReason: "家庭已承担主要意外开支",
		RiskLabel: "低", RiskReason: "当前以资金可用性为主",
		DecisionReason: "优先完成近期目标并保留随时可用的资金",
	}
}

func TestStudentCanChooseNoAutomaticEmergencyReserve(t *testing.T) {
	p := profile{IncomeCents: 250_000, Feature: "大学生，主要生活费由家里承担", OutcomeKnown: true, OutcomeCents: 200_000, HorizonMonths: 36}
	c := cashflow{Source: "conversation_categories", NeedsCents: 150_000, WantsCents: 50_000}
	base := preparePlan(p, c, nil)
	if base.SavingsCents != 50_000 || base.ReserveTarget != 0 {
		t.Fatalf("base should contain facts, not a fixed reserve: %+v", base)
	}
	proposal := testProposal()
	proposal.GoalSavingsPct = 60
	proposal.ConservativePct = 40
	got, err := applyPlanProposal(base, p, proposal)
	if err != nil {
		t.Fatal(err)
	}
	if got.ReserveTarget != 0 || got.ReserveMonthly != 0 || got.GoalSavings != 30_000 || got.Conservative != 20_000 {
		t.Fatalf("student allocation mismatch: %+v", got)
	}
}

func TestAgentMayChooseGrowthBeyondFormerFixedCap(t *testing.T) {
	p := profile{IncomeCents: 1_200_000, StableIncome: true, DebtCents: 30_000, HorizonMonths: 60, MaxLossPct: 15}
	c := cashflow{Source: "ledger", Months: 2, NeedsCents: 560_000, WantsCents: 120_000, DebtCents: 30_000}
	base := preparePlan(p, c, nil)
	proposal := testProposal()
	proposal.ConservativePct = 70
	proposal.GrowthPct = 30
	proposal.RiskLabel = "中"
	got, err := applyPlanProposal(base, p, proposal)
	if err != nil {
		t.Fatal(err)
	}
	if got.SavingsCents != 490_000 || got.Growth != 147_000 || got.StressLossPct != 10.5 {
		t.Fatalf("dynamic allocation mismatch: %+v", got)
	}
	if err := validateAgentPlan(got); err != nil {
		t.Fatal(err)
	}
}

func TestProposalRejectsLossBeyondStatedTolerance(t *testing.T) {
	p := profile{IncomeCents: 1_000_000, HorizonMonths: 60, MaxLossPct: 5}
	base := preparePlan(p, cashflow{Source: "ledger", NeedsCents: 400_000, WantsCents: 100_000}, nil)
	proposal := testProposal()
	proposal.ConservativePct = 70
	proposal.GrowthPct = 30
	if _, err := applyPlanProposal(base, p, proposal); err == nil || !strings.Contains(err.Error(), "超过用户") {
		t.Fatalf("loss budget was ignored: %v", err)
	}
}

func TestNegativeCashflowCannotAllocateMoney(t *testing.T) {
	p := profile{IncomeCents: 300_000}
	base := preparePlan(p, cashflow{Source: "ledger", NeedsCents: 350_000, DebtCents: 80_000}, nil)
	if base.SavingsCents != 0 {
		t.Fatalf("negative cashflow produced surplus: %+v", base)
	}
	proposal := testProposal()
	proposal.ReserveKind = "emergency"
	proposal.ReserveTargetYuan = 3000
	got, err := applyPlanProposal(base, p, proposal)
	if err != nil || got.InvestableCents != 0 || got.ReserveMonthly != 0 {
		t.Fatalf("zero-surplus handling failed: %+v %v", got, err)
	}
}

func TestUnstableIncomeUsesObservedLowMonth(t *testing.T) {
	p := profile{IncomeCents: 800_000, StableIncome: false}
	c := cashflow{Source: "ledger", Months: 3, MinIncomeCents: 500_000, NeedsCents: 300_000, WantsCents: 100_000}
	got := preparePlan(p, c, nil)
	if got.IncomeCents != 500_000 || got.SavingsCents != 100_000 {
		t.Fatalf("unstable income not conservatively budgeted: %+v", got)
	}
}

func TestCSVMappingAndMoney(t *testing.T) {
	raw := []byte("交易时间,收支,金额,交易说明\n2026-09-01,收入,12000.00,月度工资\n2026-09-02,支出,35.50,午餐\n")
	records, err := csvRecords(raw)
	if err != nil {
		t.Fatal(err)
	}
	got, problems, err := parseCSVTransactions(raw, autoMapping(records[0]))
	if err != nil {
		t.Fatal(err)
	}
	if len(problems) > 0 || len(got) != 2 {
		t.Fatalf("rows=%v problems=%v", got, problems)
	}
	if got[0].Direction != "income" || got[0].AmountCents != 1_200_000 || got[1].Category != "餐饮" || got[1].AmountCents != 3550 {
		t.Fatalf("unexpected rows: %+v", got)
	}
	if _, err := parseMoney("12.345"); err == nil {
		t.Fatal("accepted too many decimals")
	}
	if _, err := parseMoney("-10"); err == nil {
		t.Fatal("accepted negative amount")
	}
	if !strings.Contains(formatMoney(3550), "35.50") {
		t.Fatal("money formatting")
	}
}
