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
