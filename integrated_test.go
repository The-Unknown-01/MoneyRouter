package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"testing"
)

func TestIntegratedFragmentAndPythonContract(t *testing.T) {
	a := testApp(t)
	cookie, csrf, id := registerTestUser(t, a, "integrated_owner")
	fake := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer test" {
			t.Error("missing private token")
		}
		if strings.HasSuffix(r.URL.Path, "/state") {
			w.Write([]byte(`{"contract_version":"1","revision":1,"profile":{"income_cents":null,"max_loss_pct":0},"profile_result":{"confirmed":true,"understanding":{"income_cents":null,"max_loss_pct":0}},"categories":["餐饮","居住"],"month_current":false,"stale":true}`))
			return
		}
		if r.URL.Path == "/v1/jobs" {
			var c map[string]any
			json.NewDecoder(r.Body).Decode(&c)
			if c["user_id"] != agentUser(id) {
				t.Error("browser identity trusted")
			}
			p := obj(c["payload"])
			if p["income_cents"] != nil || num(p["max_loss_pct"]) != 0 {
				t.Error("null or zero lost")
			}
			w.WriteHeader(202)
			w.Write([]byte(`{"job_id":"job001","status":"queued","contract_version":"1"}`))
			return
		}
		w.Write([]byte(`{}`))
	}))
	defer fake.Close()
	a.bridge = &agentBridge{fake.URL, "test", fake.Client()}
	req := httptest.NewRequest("GET", "/profile", nil)
	req.AddCookie(cookie)
	req.Header.Set("HX-Request", "true")
	w := httptest.NewRecorder()
	a.routes().ServeHTTP(w, req)
	if w.Code != 200 || strings.Contains(w.Body.String(), "<!doctype") || !strings.Contains(w.Body.String(), `id="app-main"`) {
		t.Fatalf("bad fragment: %d %s", w.Code, w.Body.String())
	}
	if w.Header().Get("Cache-Control") != "no-store" || w.Header().Get("Vary") != "HX-Request" {
		t.Error("private HTML cache policy missing")
	}
	req.Header.Set("HX-History-Restore-Request", "true")
	w = httptest.NewRecorder()
	a.routes().ServeHTTP(w, req)
	if !strings.Contains(w.Body.String(), "<!doctype") || !strings.Contains(w.Body.String(), "flow-header") {
		t.Fatal("history restore must retain full page and header")
	}
	req = httptest.NewRequest("POST", "/agent/manual", strings.NewReader(url.Values{"csrf": {csrf}, "action": {"confirm"}, "max_loss_pct": {"0"}, "user_id": {"u999"}, "request_id": {"request001"}}.Encode()))
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.AddCookie(cookie)
	w = httptest.NewRecorder()
	a.routes().ServeHTTP(w, req)
	if w.Code != 200 || !strings.Contains(w.Body.String(), "every 2s") {
		t.Fatalf("job feedback missing: %d %s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), `hx-push-url="false"`) || !strings.Contains(w.Body.String(), `hx-select="unset"`) {
		t.Fatal("polling must not inherit navigation or page selection")
	}
	if _, err := a.db.Exec(`INSERT INTO transactions(user_id,date,direction,amount_cents,category,source,created_at) VALUES(?,?,?,?,?,?,?)`, id, "2026-09-01", "transfer", 10001, "转账", "manual", utcNow()); err != nil {
		t.Fatal(err)
	}
	rows, err := a.allMonthTransactions(id, "2026-09")
	if err != nil || len(rows) != 1 || rows[0].AmountCents != 10001 {
		t.Fatal("transfer/amount lost")
	}
	var output map[string]any
	if err := a.bridge.call(context.Background(), "GET", "/v1/users/u1/state", nil, &output); err != nil {
		t.Fatal(err)
	}
	if obj(output["profile"])["income_cents"] != nil {
		t.Fatal("null changed")
	}
	for _, target := range []string{"999999", strconv.FormatInt(rows[0].ID, 10)} {
		req = httptest.NewRequest("POST", "/ledger/delete", strings.NewReader(url.Values{"csrf": {csrf}, "id": {target}, "period": {"2026-09"}}.Encode()))
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		req.Header.Set("HX-Request", "true")
		req.AddCookie(cookie)
		w = httptest.NewRecorder()
		a.routes().ServeHTTP(w, req)
		expectedStatus := 200
		if target == "999999" {
			expectedStatus = 404
		}
		if w.Code != expectedStatus {
			t.Fatalf("delete failed: %d", w.Code)
		}
	}
	remaining, err := a.allMonthTransactions(id, "2026-09")
	if err != nil || len(remaining) != 0 {
		t.Fatal("delete did not update ledger")
	}
}

func TestWalletPlanRendersAmountsReasonsAndSchedule(t *testing.T) {
	a := testApp(t)
	cookie, _, _ := registerTestUser(t, a, "wallet_owner")
	fake := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"profile":{"income_cents":250000},"month_current":true,"plan_result":{"awaiting_confirmation":true,"plan":{"schema_version":2,"headline":"暑假资金安排","budget":{"income_cents":250000,"necessary_cents":90000,"wants_cents":40000,"savings_cents":120000},"wallets":[{"id":"food","name":"餐饮钱包","kind":"expense","category":"餐饮","amount_cents":90000,"spent_cents":50000,"remaining_cents":40000,"reason":"覆盖已花并预留后续餐饮","execution":"按剩余周数核对"},{"id":"invest","name":"投资钱包","kind":"investment","amount_cents":120000,"investment_pct":100,"reason":"资金期限允许","execution":"分批投入","asset_scope":"分散债券类别","horizon_months":24,"liquidity":"保留日常资金","steps":[{"date":"2026-07-15","amount_cents":120000}],"risks":["净值波动"],"review_conditions":["期限变化时评估"]}]}}}`))
	}))
	defer fake.Close()
	a.bridge = &agentBridge{fake.URL, "test", fake.Client()}
	req := httptest.NewRequest("GET", "/plan?period=2026-07", nil)
	req.AddCookie(cookie)
	w := httptest.NewRecorder()
	a.routes().ServeHTTP(w, req)
	html := w.Body.String()
	for _, expected := range []string{"本月钱包计划", "wallet-row", "data-chart=\"wallets\"", "¥900.00", "¥400.00", "覆盖已花", "2026-07-15", "确认这份方案"} {
		if w.Code != 200 || !strings.Contains(html, expected) {
			t.Fatalf("wallet page missing %q: %d %s", expected, w.Code, html)
		}
	}
}

func TestMonthCanStartWithoutLedgerRows(t *testing.T) {
	a := testApp(t)
	cookie, csrf, _ := registerTestUser(t, a, "spoken_month")
	called := false
	fake := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/state") {
			w.Write([]byte(`{"profile":{"income_cents":250000},"revision":1}`))
			return
		}
		if r.URL.Path == "/v1/jobs" {
			called = true
			var command map[string]any
			json.NewDecoder(r.Body).Decode(&command)
			if str(command["kind"]) != "month" {
				t.Error("wrong agent")
			}
			w.WriteHeader(202)
			w.Write([]byte(`{"job_id":"spoken-job","status":"queued"}`))
		}
	}))
	defer fake.Close()
	a.bridge = &agentBridge{fake.URL, "test", fake.Client()}
	req := httptest.NewRequest("POST", "/agent/month", strings.NewReader(url.Values{"csrf": {csrf}, "period": {"2026-07"}, "message": {"本月收入2500，已花800"}}.Encode()))
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.AddCookie(cookie)
	w := httptest.NewRecorder()
	a.routes().ServeHTTP(w, req)
	if !called || w.Code != 200 {
		t.Fatalf("spoken month blocked: %d %s", w.Code, w.Body.String())
	}
}
