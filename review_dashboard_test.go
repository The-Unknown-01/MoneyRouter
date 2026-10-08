package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func reviewFixture(t *testing.T) map[string]any {
	t.Helper()
	raw, err := os.ReadFile("testdata/review_dashboard.json")
	if err != nil {
		t.Fatal(err)
	}
	var summary map[string]any
	if err := json.Unmarshal(raw, &summary); err != nil {
		t.Fatal(err)
	}
	return summary
}

func TestReviewDashboardFinancialEvidence(t *testing.T) {
	summary := reviewFixture(t)
	d := buildReviewDashboard(summary)
	if d.GoalPercent != "112.0%" || d.GoalBar != 100 || d.GoalStatus != "本月储蓄目标已达成" {
		t.Fatalf("wrong goal: %+v", d)
	}
	if d.Metrics[4].Value != formatMoney(62000) || d.Metrics[4].Label != "比上月少花" {
		t.Fatal("expense saving must use spending delta only")
	}
	if d.Surplus != formatMoney(280000) || d.MarketReturn != formatMoney(12800) {
		t.Fatal("cashflow and market returns mixed")
	}
	if d.Wallets[2].Tone != "caution" || d.Wallets[4].Status != "计划投入已完成" {
		t.Fatal("wallet kinds must have distinct execution semantics")
	}
	i := obj(summary["insights"])
	summary["review_mode"], i["review_mode"], i["comparable"] = "stage", "stage", false
	d = buildReviewDashboard(summary)
	if strings.Contains(d.GoalStatus, "已达成") || len(d.Milestones) > 0 || d.Metrics[4].Value != "暂不可比较" {
		t.Fatal("partial month must not claim final achievements or savings")
	}
	i["comparable"] = true
	summary["review_mode"] = "final"
	obj(arr(i["metrics"])[1])["delta_cents"] = float64(5000)
	d = buildReviewDashboard(summary)
	if d.Metrics[4].Label != "比上月多花" {
		t.Fatal("higher expense misrepresented as saving")
	}
}

func TestReviewDashboardNullZeroAndEscaping(t *testing.T) {
	summary := map[string]any{"review_mode": "final", "insights": map[string]any{"metrics": []any{map[string]any{"id": "investment_return_cents", "current_cents": float64(0)}}}}
	d := buildReviewDashboard(summary)
	if d.Metrics[3].Value != formatMoney(0) || d.Metrics[0].Value != "尚未了解" || d.GoalKnown {
		t.Fatal("null and zero must remain distinct")
	}
	obj(summary["insights"])["goal"] = map[string]any{"goal": "<script>alert(1)</script>", "saved_this_month_cents": float64(-1000), "planned_this_month_cents": float64(10000)}
	d = buildReviewDashboard(summary)
	if d.GoalBar != 0 || d.GoalPercent != "-10.0%" {
		t.Fatal("negative result must stay negative while progress bar stays bounded")
	}
	var out bytes.Buffer
	if err := newTemplates().ExecuteTemplate(&out, "review_dashboard", webData{Review: d}); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(out.String(), "<script>alert") || !strings.Contains(out.String(), "&lt;script&gt;") {
		t.Fatal("agent text must be escaped")
	}
}

func TestReviewDashboardRouteAndPreview(t *testing.T) {
	summary := reviewFixture(t)
	history := []any{}
	for _, p := range []string{"2026-06", "2026-07", "2026-08", "2026-09"} {
		history = append(history, map[string]any{"period": p, "snapshot": map[string]any{"schema_version": 2, "coverage_complete": true, "income": map[string]any{"role": "actual", "amount_cents": 650000}, "spend_total_cents": 462000, "balance_cents": 188000, "investments": map[string]any{"month_return_cents": -5600}}})
	}
	obj(history[3])["snapshot"] = map[string]any{"schema_version": 2, "coverage_complete": true, "income": map[string]any{"role": "actual", "amount_cents": 680000}, "spend_total_cents": 400000, "balance_cents": 280000, "investments": map[string]any{"month_return_cents": 12800}}
	state := map[string]any{"profile": map[string]any{"goal": "应急储蓄"}, "review_mode": "final", "history": history, "history_periods": []string{"2026-09", "2026-08"}, "summary_result": map[string]any{"summary": summary, "confirmed": true}, "versions": []any{map[string]any{"version": 1}}}
	a := testApp(t)
	cookie, _, _ := registerTestUser(t, a, "review_owner")
	fake := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { json.NewEncoder(w).Encode(state) }))
	defer fake.Close()
	a.bridge = &agentBridge{fake.URL, "test", fake.Client()}
	for _, fragment := range []bool{false, true} {
		req := httptest.NewRequest("GET", "/review?period=2026-09", nil)
		req.AddCookie(cookie)
		if fragment {
			req.Header.Set("HX-Request", "true")
		}
		w := httptest.NewRecorder()
		a.routes().ServeHTTP(w, req)
		if w.Code != 200 {
			t.Fatalf("review route: %d", w.Code)
		}
		for _, want := range []string{"112.0%", "比上月少花", "投资盈亏", "钱包执行情况", "review-comparison", "review-history", "current_cents", "4.6%", "13.4%"} {
			if !strings.Contains(w.Body.String(), want) {
				t.Fatalf("missing %s", want)
			}
		}
		if !fragment {
			if dir := os.Getenv("REVIEW_PREVIEW_DIR"); dir != "" {
				if err := os.MkdirAll(dir, 0700); err != nil {
					t.Fatal(err)
				}
				html := strings.Replace(w.Body.String(), "<body class=\"flow-body\"", "<body data-preview=\"synthetic\" class=\"flow-body\"", 1)
				html = strings.Replace(html, "<main class=\"flow-main\"", "<p class=\"notice\">界面验收预览 · 演示数据，不代表真实用户结果</p><main class=\"flow-main\"", 1)
				if err := os.WriteFile(filepath.Join(dir, "review-preview.html"), []byte(html), 0600); err != nil {
					t.Fatal(err)
				}
			}
		}
	}
}
