package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestExplicitMonthValidationAndCalendar(t *testing.T) {
	if nextPeriod("2026-12") != "2027-01" || nextPeriod("9999-12") != "" || validMonth("0000-01") {
		t.Fatal("invalid calendar boundary")
	}
	if periodOf(httptest.NewRequest("GET", "/plan?period=2026-13", nil)) != "2026-13" {
		t.Fatal("invalid explicit period silently became current month")
	}
	if periodOf(httptest.NewRequest("GET", "/plan", nil)) != businessNow().Format("2006-01") {
		t.Fatal("missing period did not use China current month")
	}
}

func TestMonthSemanticsPages(t *testing.T) {
	a := testApp(t)
	cookie, _, _ := registerTestUser(t, a, "month_labels")
	first, _ := time.Parse("2006-01", businessNow().Format("2006-01"))
	past, future := first.AddDate(0, -1, 0).Format("2006-01"), first.AddDate(0, 1, 0).Format("2006-01")
	fake := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{"profile": map[string]any{}, "categories": []any{},
			"history_periods": []string{past}, "review_mode": "stage", "month_result": map[string]any{"reply": "请核对资料"},
			"versions": []any{map[string]any{"version": 1}}, "summary_result": map[string]any{}})
	}))
	defer fake.Close()
	a.bridge = &agentBridge{fake.URL, "test", fake.Client()}
	for _, c := range []struct {
		path         string
		want, absent []string
	}{
		{"/plan?period=" + past, []string{past + " 资金方案", "历史回看", "方案月份"}, []string{"生成方案", "本月钱包计划"}},
		{"/month?period=" + future, []string{future + " 月度核对", "预计整月总收入", "expected_income_cents"}, []string{"name=\"income_cents\"", "整月全部收支"}},
		{"/month?period=" + past, []string{past + " 已到账实际收入", "核对月份", "coverage_complete"}, []string{"本月预计收入"}},
		{"/review?period=" + past, []string{past + " 阶段回顾", "复盘月份", "plan_version", "核对 " + nextPeriod(past) + " 资料"}, []string{"回看这个月"}},
	} {
		t.Run(c.path, func(t *testing.T) {
			r := httptest.NewRequest("GET", c.path, nil)
			r.AddCookie(cookie)
			w := httptest.NewRecorder()
			a.routes().ServeHTTP(w, r)
			body := w.Body.String()
			if w.Code != 200 {
				t.Fatalf("status %d: %s", w.Code, body)
			}
			for _, s := range c.want {
				if !strings.Contains(body, s) {
					t.Errorf("missing %q", s)
				}
			}
			for _, s := range c.absent {
				if strings.Contains(body, s) {
					t.Errorf("unexpected %q", s)
				}
			}
		})
	}
	invalid := httptest.NewRequest("GET", "/ledger?period=2026-13", nil)
	invalid.AddCookie(cookie)
	w := httptest.NewRecorder()
	a.routes().ServeHTTP(w, invalid)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("invalid month status: %d", w.Code)
	}
}

func TestFutureActualTransactionIsRejected(t *testing.T) {
	a := testApp(t)
	cookie, csrf, id := registerTestUser(t, a, "future_actual")
	date := businessNow().AddDate(0, 0, 1).Format("2006-01-02")
	values := url.Values{"csrf": {csrf}, "period": {date[:7]}, "date": {date}, "direction": {"expense"}, "amount": {"100"}, "category": {"餐饮"}}
	r := httptest.NewRequest("POST", "/ledger", strings.NewReader(values.Encode()))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	r.AddCookie(cookie)
	w := httptest.NewRecorder()
	// No agent service is needed: the date must be rejected before mutations.
	a.bridge = &agentBridge{"http://127.0.0.1:1", "", &http.Client{}}
	a.routes().ServeHTTP(w, r)
	var count int
	a.db.QueryRow("SELECT count(*) FROM transactions WHERE user_id=?", id).Scan(&count)
	if count != 0 || !strings.Contains(w.Body.String(), "未来费用") {
		t.Fatalf("future transaction was not rejected: %s", w.Body.String())
	}
}
