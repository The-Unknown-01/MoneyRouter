package main

import (
	"strings"
	"testing"
)

func TestPlanMatchesApprovedDefaults(t *testing.T) {
	p := profile{IncomeCents: 1_200_000, StableIncome: true, FamilyLoad: false, DebtCents: 30_000, ReserveCents: 1_500_000, HorizonMonths: 60, MaxLossPct: 10, Experience: "some"}
	c := cashflow{Months: 2, Count: 16, IncomeCents: 1_200_000, NeedsCents: 560_000, WantsCents: 120_000, DebtCents: 30_000}
	got := computePlan(p, c, nil)
	if got.ReserveMonths != 3 || got.ReserveTarget != 1_680_000 || got.ReserveGap != 180_000 {
		t.Fatalf("reserve mismatch: %+v", got)
	}
	if got.SavingsCents != 490_000 || got.InvestableCents != 310_000 {
		t.Fatalf("cash balance mismatch: %+v", got)
	}
	if got.Risk != "中" || got.GrowthCapPct != 20 || got.Growth != 62_000 {
		t.Fatalf("risk allocation mismatch: %+v", got)
	}
	if err := validatePlan(got); err != nil {
		t.Fatal(err)
	}
}

func TestUnstableIncomeUsesSixMonthsAndNoGrowthWhenReserveMissing(t *testing.T) {
	p := profile{IncomeCents: 500_000, StableIncome: false, FamilyLoad: true, HorizonMonths: 120, MaxLossPct: 30, Experience: "experienced"}
	c := cashflow{Months: 1, Count: 8, NeedsCents: 300_000, WantsCents: 100_000}
	got := computePlan(p, c, nil)
	if got.ReserveMonths != 6 || got.ReserveTarget != 1_800_000 {
		t.Fatalf("wrong reserve: %+v", got)
	}
	if got.InvestableCents != 0 || got.Growth != 0 {
		t.Fatalf("should fund reserve first: %+v", got)
	}
	if got.Risk != "中" {
		t.Fatalf("unstable income should cap risk at medium: %+v", got)
	}
}

func TestUnstableIncomeUsesObservedLowMonth(t *testing.T) {
	p := profile{IncomeCents: 800_000, StableIncome: false, ReserveCents: 2_000_000, HorizonMonths: 60, MaxLossPct: 20, Experience: "some"}
	c := cashflow{Months: 3, Count: 20, IncomeCents: 900_000, MinIncomeCents: 500_000, NeedsCents: 300_000, WantsCents: 100_000}
	got := computePlan(p, c, nil)
	if got.IncomeCents != 500_000 || got.SavingsCents != 100_000 {
		t.Fatalf("unstable income not conservatively budgeted: %+v", got)
	}
	if err := validatePlan(got); err != nil {
		t.Fatal(err)
	}
}

func TestNegativeCashflowDoesNotCreateInvestment(t *testing.T) {
	p := profile{IncomeCents: 300_000, StableIncome: true, HorizonMonths: 120, MaxLossPct: 30, Experience: "experienced"}
	c := cashflow{Months: 1, Count: 5, NeedsCents: 350_000, DebtCents: 80_000}
	got := computePlan(p, c, nil)
	if got.SavingsCents != 0 || got.InvestableCents != 0 || got.Growth != 0 {
		t.Fatalf("negative cashflow produced investment: %+v", got)
	}
	if err := validatePlan(got); err != nil {
		t.Fatal(err)
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
