package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"net/url"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func testApp(t *testing.T) *app {
	t.Helper()
	db, err := openDatabase(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return &app{db: db, tpl: newTemplates(), deepseek: &deepseekClient{}, news: &newsClient{expires: time.Now().Add(time.Hour)}, sem: make(chan struct{}, 2), previews: newPreviewStore(), authRate: newRateLimiter()}
}

func request(t *testing.T, a *app, method, path string, values url.Values, cookie *http.Cookie) *httptest.ResponseRecorder {
	t.Helper()
	body := ""
	if values != nil {
		body = values.Encode()
	}
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if values != nil {
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	}
	if cookie != nil {
		req.AddCookie(cookie)
	}
	w := httptest.NewRecorder()
	a.routes().ServeHTTP(w, req)
	return w
}

func registerTestUser(t *testing.T, a *app, name string) (*http.Cookie, string, int64) {
	t.Helper()
	w := request(t, a, "POST", "/register", url.Values{"username": {name}, "password": {"testpass123"}}, nil)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("register %s: %d %s", name, w.Code, w.Body.String())
	}
	var cookie *http.Cookie
	for _, c := range w.Result().Cookies() {
		if c.Name == "finance_session" {
			cookie = c
		}
	}
	if cookie == nil {
		t.Fatal("missing session cookie")
	}
	var id int64
	var csrf string
	if err := a.db.QueryRow(`SELECT u.id,s.csrf FROM users u JOIN sessions s ON s.user_id=u.id WHERE u.username=?`, name).Scan(&id, &csrf); err != nil {
		t.Fatal(err)
	}
	return cookie, csrf, id
}

func confirmTestProfile(t *testing.T, a *app, cookie *http.Cookie, csrf string) {
	t.Helper()
	w := request(t, a, "POST", "/profile", url.Values{
		"csrf": {csrf}, "income": {"12000"}, "debt": {"300"}, "reserve": {"15000"},
		"horizon": {"60"}, "loss": {"10"}, "experience": {"some"},
		"stable": {"yes"}, "family": {"no"}, "goal": {"稳健积累资产"}, "confirm": {"yes"},
	}, cookie)
	if w.Code != http.StatusSeeOther || !strings.HasPrefix(w.Header().Get("Location"), "/ledger") {
		t.Fatalf("confirm profile: %d %s", w.Code, w.Header().Get("Location"))
	}
}

func TestAccountsAreIsolatedAcrossRoutes(t *testing.T) {
	a := testApp(t)
	ac, at, aid := registerTestUser(t, a, "account_a")
	bc, bt, bid := registerTestUser(t, a, "account_b")
	confirmTestProfile(t, a, ac, at)
	confirmTestProfile(t, a, bc, bt)
	seed := request(t, a, "POST", "/demo", url.Values{"csrf": {at}}, ac)
	if seed.Code != http.StatusSeeOther {
		t.Fatalf("seed: %d %s", seed.Code, seed.Body.String())
	}
	var txID int64
	if err := a.db.QueryRow(`SELECT id FROM transactions WHERE user_id=? LIMIT 1`, aid).Scan(&txID); err != nil {
		t.Fatal(err)
	}
	ledgerB := request(t, a, "GET", "/ledger", nil, bc)
	if ledgerB.Code != 200 || strings.Contains(ledgerB.Body.String(), "月度工资") {
		t.Fatal("account B can see A's ledger")
	}
	deleteB := request(t, a, "POST", "/ledger/delete", url.Values{"csrf": {bt}, "id": {fmt.Sprint(txID)}}, bc)
	if deleteB.Code != http.StatusNotFound {
		t.Fatalf("cross-account delete returned %d", deleteB.Code)
	}
	var remains int
	if err := a.db.QueryRow(`SELECT count(*) FROM transactions WHERE user_id=? AND id=?`, aid, txID).Scan(&remains); err != nil || remains != 1 {
		t.Fatal("A's transaction changed")
	}
	clearB := request(t, a, "POST", "/clear", url.Values{"csrf": {bt}, "confirm": {"CLEAR"}}, bc)
	if clearB.Code != http.StatusSeeOther {
		t.Fatalf("clear B %d", clearB.Code)
	}
	var countA, countB int
	_ = a.db.QueryRow(`SELECT count(*) FROM transactions WHERE user_id=?`, aid).Scan(&countA)
	_ = a.db.QueryRow(`SELECT count(*) FROM transactions WHERE user_id=?`, bid).Scan(&countB)
	if countA == 0 || countB != 0 {
		t.Fatalf("isolation after clear: A=%d B=%d", countA, countB)
	}
	_ = at
}

func TestPlanNeedsConfirmedProfileAndRejectsWrongCSRF(t *testing.T) {
	a := testApp(t)
	cookie, csrf, _ := registerTestUser(t, a, "account_c")
	w := request(t, a, "POST", "/demo", url.Values{"csrf": {"wrong"}}, cookie)
	if w.Code != http.StatusForbidden {
		t.Fatalf("wrong csrf returned %d", w.Code)
	}
	w = request(t, a, "POST", "/plan/generate", url.Values{"csrf": {csrf}}, cookie)
	if w.Code != http.StatusSeeOther || !strings.Contains(w.Header().Get("Location"), "/profile") {
		t.Fatalf("missing profile not handled: %d", w.Code)
	}
}

func TestCSVPreviewRequiresOwnerAndConfirmation(t *testing.T) {
	a := testApp(t)
	ac, at, aid := registerTestUser(t, a, "csv_owner")
	bc, bt, _ := registerTestUser(t, a, "csv_other")
	confirmTestProfile(t, a, ac, at)
	confirmTestProfile(t, a, bc, bt)
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	if err := writer.WriteField("csrf", at); err != nil {
		t.Fatal(err)
	}
	file, err := writer.CreateFormFile("file", "bill.csv")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = file.Write([]byte("日期,收支,金额,分类,描述\n2026-09-05,收入,12000.00,工资,月薪\n2026-09-06,支出,100.00,餐饮,午餐\n"))
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest("POST", "/ledger/import", &body)
	req.Header.Set("Content-Type", writer.FormDataContentType())
	req.AddCookie(ac)
	w := httptest.NewRecorder()
	a.routes().ServeHTTP(w, req)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("upload: %d %s", w.Code, w.Body.String())
	}
	location := w.Header().Get("Location")
	if !strings.HasPrefix(location, "/ledger/preview?id=") {
		t.Fatalf("wrong redirect: %s", location)
	}
	preview := request(t, a, "GET", location, nil, ac)
	if preview.Code != 200 || !strings.Contains(preview.Body.String(), "可导入 2 笔") {
		t.Fatalf("preview failed: %d", preview.Code)
	}
	other := request(t, a, "GET", location, nil, bc)
	if other.Code != http.StatusSeeOther {
		t.Fatalf("other user accessed preview: %d", other.Code)
	}
	id := strings.TrimPrefix(location, "/ledger/preview?id=")
	confirmed := request(t, a, "POST", "/ledger/confirm", url.Values{"csrf": {at}, "id": {id}}, ac)
	if confirmed.Code != http.StatusSeeOther {
		t.Fatalf("confirm: %d %s", confirmed.Code, confirmed.Body.String())
	}
	var count int
	if err := a.db.QueryRow(`SELECT count(*) FROM transactions WHERE user_id=?`, aid).Scan(&count); err != nil || count != 2 {
		t.Fatalf("count=%d err=%v", count, err)
	}
}

func TestTwoAccountsCompleteFlowAndBackup(t *testing.T) {
	a := testApp(t)
	ac, at, aid := registerTestUser(t, a, "flow_account_a")
	bc, bt, _ := registerTestUser(t, a, "flow_account_b")
	for _, account := range []struct {
		cookie *http.Cookie
		csrf   string
	}{{ac, at}, {bc, bt}} {
		start := request(t, a, "GET", "/", nil, account.cookie)
		if start.Code != http.StatusSeeOther || start.Header().Get("Location") != "/profile" {
			t.Fatalf("new user did not start at profile: %d %s", start.Code, start.Header().Get("Location"))
		}
		for _, endpoint := range []string{"/profile"} {
			w := request(t, a, "GET", endpoint, nil, account.cookie)
			if w.Code != 200 || strings.Contains(w.Body.String(), "ZgotmplZ") {
				t.Fatalf("page %s returned %d: %s", endpoint, w.Code, w.Body.String())
			}
			if strings.Contains(w.Body.String(), "class=\"sidebar\"") || strings.Contains(w.Body.String(), "财务总览") {
				t.Fatal("legacy dashboard navigation remains visible")
			}
		}
		confirmTestProfile(t, a, account.cookie, account.csrf)
		if w := request(t, a, "GET", "/plan", nil, account.cookie); w.Code != http.StatusSeeOther || w.Header().Get("Location") != "/ledger" {
			t.Fatalf("plan before ledger: %d %s", w.Code, w.Header().Get("Location"))
		}
		if w := request(t, a, "GET", "/ledger", nil, account.cookie); w.Code != 200 {
			t.Fatalf("ledger page: %d %s", w.Code, w.Body.String())
		}
		if w := request(t, a, "GET", "/review", nil, account.cookie); w.Code != http.StatusSeeOther || w.Header().Get("Location") != "/ledger" {
			t.Fatalf("review before plan: %d %s", w.Code, w.Header().Get("Location"))
		}
		if w := request(t, a, "POST", "/demo", url.Values{"csrf": {account.csrf}}, account.cookie); w.Code != http.StatusSeeOther {
			t.Fatalf("demo: %d", w.Code)
		}
		if w := request(t, a, "GET", "/", nil, account.cookie); w.Code != http.StatusSeeOther || w.Header().Get("Location") != "/plan" {
			t.Fatalf("account did not advance to plan: %d %s", w.Code, w.Header().Get("Location"))
		}
		if w := request(t, a, "GET", "/review", nil, account.cookie); w.Code != http.StatusSeeOther || w.Header().Get("Location") != "/plan" {
			t.Fatalf("review without a saved plan: %d %s", w.Code, w.Header().Get("Location"))
		}
		if w := request(t, a, "POST", "/plan/generate", url.Values{"csrf": {account.csrf}}, account.cookie); w.Code != http.StatusSeeOther {
			t.Fatalf("plan: %d %s", w.Code, w.Body.String())
		}
		for _, endpoint := range []string{"/plan", "/ledger", "/chat", "/review?month=" + time.Now().AddDate(0, -1, 0).Format("2006-01")} {
			if w := request(t, a, "GET", endpoint, nil, account.cookie); w.Code != 200 || strings.Contains(w.Body.String(), "页面渲染失败") {
				t.Fatalf("result %s: %d", endpoint, w.Code)
			}
		}
	}
	if err := a.addMessage(aid, "plan", "user", "private-marker-a"); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(request(t, a, "GET", "/chat", nil, bc).Body.String(), "private-marker-a") {
		t.Fatal("account B saw account A's conversation")
	}
	if err := a.db.backup(filepath.Join(t.TempDir(), "snapshot.db")); err != nil {
		t.Fatal(err)
	}
}

func TestPlanRejectsStaleVersion(t *testing.T) {
	a := testApp(t)
	_, _, id := registerTestUser(t, a, "version_owner")
	p := computePlan(profile{IncomeCents: 100_000, StableIncome: true}, cashflow{Months: 1, Count: 1}, nil)
	if err := a.savePlan(id, 0, &p); err != nil {
		t.Fatal(err)
	}
	if err := a.savePlan(id, 0, &p); err != errPlanVersionConflict {
		t.Fatalf("stale version accepted: %v", err)
	}
}

func TestProfileAgentExtractsDraftForCurrentAccount(t *testing.T) {
	a := testApp(t)
	ac, at, aid := registerTestUser(t, a, "draft_owner")
	_, _, otherID := registerTestUser(t, a, "draft_other")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var requestBody map[string]any
		if err := json.NewDecoder(r.Body).Decode(&requestBody); err != nil {
			t.Error(err)
		}
		if requestBody["response_format"] == nil {
			t.Error("profile extraction did not request structured output")
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"choices": []any{map[string]any{"message": map[string]any{"role": "assistant", "content": `{"income_yuan":8500,"stable_income":false,"max_loss_pct":5,"goal":"先建立预备金","message":"已记录","next_question":"请核对表单"}`}}}})
	}))
	defer server.Close()
	a.deepseek = &deepseekClient{key: "test", base: server.URL, http: server.Client(), sem: make(chan struct{}, 2)}
	w := request(t, a, "POST", "/profile/ask", url.Values{"csrf": {at}, "message": {"我月收入8500元，不稳定，最多亏5%，先建立预备金"}}, ac)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("agent answer: %d %s", w.Code, w.Body.String())
	}
	p, err := a.getProfile(aid)
	if err != nil || p.IncomeCents != 850000 || p.StableIncome || p.MaxLossPct != 5 || p.Confirmed {
		t.Fatalf("wrong draft: %+v %v", p, err)
	}
	other, err := a.getProfile(otherID)
	if err != nil || other.IncomeCents != 0 {
		t.Fatalf("other account changed: %+v %v", other, err)
	}
}

func TestConcurrentAccountsKeepLedgersSeparate(t *testing.T) {
	a := testApp(t)
	ac, at, aid := registerTestUser(t, a, "parallel_a")
	bc, bt, bid := registerTestUser(t, a, "parallel_b")
	confirmTestProfile(t, a, ac, at)
	confirmTestProfile(t, a, bc, bt)
	type account struct {
		cookie *http.Cookie
		csrf   string
		name   string
	}
	accounts := []account{{ac, at, "a"}, {bc, bt, "b"}}
	var wg sync.WaitGroup
	results := make(chan int, 40)
	for _, acct := range accounts {
		for i := 0; i < 20; i++ {
			wg.Add(1)
			go func(account account, n int) {
				defer wg.Done()
				w := request(t, a, "POST", "/ledger", url.Values{"csrf": {account.csrf}, "date": {"2026-09-01"}, "direction": {"expense"}, "amount": {"1.00"}, "category": {"餐饮"}, "description": {fmt.Sprintf("%s-%d", account.name, n)}}, account.cookie)
				results <- w.Code
			}(acct, i)
		}
	}
	wg.Wait()
	close(results)
	for status := range results {
		if status != http.StatusSeeOther {
			t.Fatalf("concurrent save returned %d", status)
		}
	}
	for _, id := range []int64{aid, bid} {
		var count int
		if err := a.db.QueryRow(`SELECT count(*) FROM transactions WHERE user_id=?`, id).Scan(&count); err != nil || count != 20 {
			t.Fatalf("account %d got %d rows: %v", id, count, err)
		}
	}
}
