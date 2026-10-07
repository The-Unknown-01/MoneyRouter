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

func installTestPlanAgent(t *testing.T, a *app, finalReply string) {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		messages, _ := body["messages"].([]any)
		for _, raw := range messages {
			m, _ := raw.(map[string]any)
			if m["role"] == "tool" {
				_ = json.NewEncoder(w).Encode(map[string]any{"id": "test_plan_final", "choices": []any{map[string]any{"message": map[string]any{"role": "assistant", "content": finalReply}, "finish_reason": "stop"}}})
				return
			}
		}
		proposal := `{"reserve_target_yuan":0,"reserve_kind":"none","reserve_reason":"已结合当前流动性和个人责任","reserve_pct":0,"extra_debt_pct":0,"goal_savings_pct":0,"conservative_pct":100,"steady_pct":0,"growth_pct":0,"risk_label":"低","risk_reason":"当前优先保持资金可用","decision_reason":"以可用资金承接近期生活目标"}`
		call := map[string]any{"id": "call_submit", "type": "function", "function": map[string]any{"name": "submit_plan", "arguments": proposal}}
		message := map[string]any{"role": "assistant", "content": "", "tool_calls": []any{call}}
		_ = json.NewEncoder(w).Encode(map[string]any{"id": "test_plan_call", "choices": []any{map[string]any{"message": message, "finish_reason": "tool_calls"}}})
	}))
	t.Cleanup(server.Close)
	a.deepseek = &deepseekClient{key: "test", base: server.URL, http: server.Client(), sem: make(chan struct{}, 2)}
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

func TestUnavailablePlanAgentDoesNotSaveFixedAllocation(t *testing.T) {
	a := testApp(t)
	cookie, csrf, id := registerTestUser(t, a, "no_agent_plan")
	w := request(t, a, "POST", "/profile", url.Values{"csrf": {csrf}, "income": {"2500"}, "outcome": {"2000"}, "feature": {"大学生，家庭承担住宿"}, "confirm": {"yes"}}, cookie)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("profile confirmation failed: %d", w.Code)
	}
	request(t, a, "POST", "/ledger/skip", url.Values{"csrf": {csrf}}, cookie)
	w = request(t, a, "POST", "/plan/generate", url.Values{"csrf": {csrf}}, cookie)
	if w.Code != http.StatusSeeOther || !strings.Contains(w.Header().Get("Location"), "error=") {
		t.Fatalf("agent outage was not reported: %d %s", w.Code, w.Header().Get("Location"))
	}
	latest, err := a.latestPlan(id)
	if err != nil || latest != nil {
		t.Fatalf("fixed fallback allocation was saved: %+v %v", latest, err)
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
	installTestPlanAgent(t, a, "已提交方案。")
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
	p := preparePlan(profile{IncomeCents: 100_000, StableIncome: true}, cashflow{Source: "unknown"}, nil)
	if err := a.savePlan(id, 0, &p); err != nil {
		t.Fatal(err)
	}
	if err := a.savePlan(id, 0, &p); err != errPlanVersionConflict {
		t.Fatalf("stale version accepted: %v", err)
	}
}

func TestLegacyFixedPlanRequiresRegeneration(t *testing.T) {
	a := testApp(t)
	cookie, csrf, id := registerTestUser(t, a, "legacy_plan")
	request(t, a, "POST", "/profile", url.Values{"csrf": {csrf}, "income": {"2500"}, "outcome": {"2000"}, "feature": {"大学生"}, "confirm": {"yes"}}, cookie)
	request(t, a, "POST", "/ledger/skip", url.Values{"csrf": {csrf}}, cookie)
	old := plan{Algorithm: "budget-workflow-v1", IncomeCents: 250_000, SavingsCents: 50_000, GrowthCapPct: 20}
	if err := a.savePlan(id, 0, &old); err != nil {
		t.Fatal(err)
	}
	page := request(t, a, "GET", "/plan", nil, cookie)
	if page.Code != 200 || !strings.Contains(page.Body.String(), "旧版方案需要重新生成") || strings.Contains(page.Body.String(), "增长类上限") {
		t.Fatalf("legacy plan remained active: %d %s", page.Code, page.Body.String())
	}
	if review := request(t, a, "GET", "/review", nil, cookie); review.Code != http.StatusSeeOther || review.Header().Get("Location") != "/plan" {
		t.Fatalf("legacy optional page was accessible: %d %s", review.Code, review.Header().Get("Location"))
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
		if requestBody["tools"] == nil {
			t.Error("profile agent did not expose its fact recording tool")
		}
		messages, _ := requestBody["messages"].([]any)
		for _, raw := range messages {
			m, _ := raw.(map[string]any)
			if m["role"] == "tool" {
				_ = json.NewEncoder(w).Encode(map[string]any{"id": "test_response_2", "choices": []any{
					map[string]any{"index": 0, "message": map[string]any{"role": "assistant", "content": "你每月花销大致是多少？"}, "finish_reason": "stop"},
				}})
				return
			}
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"id": "test_response_1", "choices": []any{
			map[string]any{"index": 0, "message": map[string]any{"role": "assistant", "content": "", "tool_calls": []any{
				map[string]any{"id": "call_profile_1", "type": "function", "function": map[string]any{"name": "record_profile_facts", "arguments": `{"income_yuan":8500,"outcome_yuan":6000,"stable_income":false,"max_loss_pct":5,"feature":"自由职业，先建立预备金"}`}},
			}}, "finish_reason": "tool_calls"},
		}})
	}))
	defer server.Close()
	a.deepseek = &deepseekClient{key: "test", base: server.URL, http: server.Client(), sem: make(chan struct{}, 2)}
	w := request(t, a, "POST", "/profile/ask", url.Values{"csrf": {at}, "message": {"我是自由职业者，月收入8500元，花6000元，最多亏5%，先建立预备金"}}, ac)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("agent answer: %d %s", w.Code, w.Body.String())
	}
	p, err := a.getProfile(aid)
	if err != nil || p.IncomeCents != 850000 || p.OutcomeCents != 600000 || !p.OutcomeKnown || p.StableIncome || p.MaxLossPct != 5 || p.Feature == "" || p.Confirmed {
		messages, _ := a.listMessages(aid, "profile", 5)
		t.Logf("profile conversation: %+v", messages)
		t.Fatalf("wrong draft: %+v %v", p, err)
	}
	other, err := a.getProfile(otherID)
	if err != nil || other.IncomeCents != 0 {
		t.Fatalf("other account changed: %+v %v", other, err)
	}
}

func TestProfileAgentPrefillsPartialExpenseCategories(t *testing.T) {
	a := testApp(t)
	cookie, csrf, id := registerTestUser(t, a, "category_owner")
	_, _, otherID := registerTestUser(t, a, "category_other")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		messages, _ := body["messages"].([]any)
		for _, raw := range messages {
			m, _ := raw.(map[string]any)
			if m["role"] == "tool" {
				_ = json.NewEncoder(w).Encode(map[string]any{"id": "profile_category_final", "choices": []any{map[string]any{"message": map[string]any{"role": "assistant", "content": "我可以给你出每月怎么分的具体方案。"}, "finish_reason": "stop"}}})
				return
			}
		}
		calls := []any{
			map[string]any{"id": "facts", "type": "function", "function": map[string]any{"name": "record_profile_facts", "arguments": `{"income_yuan":2500,"outcome_yuan":2000,"feature":"大学生，家里承担房租，想攒旅行费","income_source":"家庭生活费"}`}},
			map[string]any{"id": "categories", "type": "function", "function": map[string]any{"name": "record_monthly_expenses", "arguments": `{"items":[{"category":"餐饮","amount_yuan":900},{"category":"娱乐","amount_yuan":100}]}`}},
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"id": "profile_category_calls", "choices": []any{map[string]any{"message": map[string]any{"role": "assistant", "tool_calls": calls}, "finish_reason": "tool_calls"}}})
	}))
	defer server.Close()
	a.deepseek = &deepseekClient{key: "test", base: server.URL, http: server.Client(), sem: make(chan struct{}, 2)}
	w := request(t, a, "POST", "/profile/ask", url.Values{"csrf": {csrf}, "message": {"我是大学生，每月生活费2500，花2000，餐饮900、娱乐100，家里承担房租"}}, cookie)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("profile answer: %d %s", w.Code, w.Body.String())
	}
	messages, _ := a.listMessages(id, "profile", 5)
	if len(messages) < 2 || strings.Contains(messages[len(messages)-1].Content, "怎么分") {
		t.Fatalf("profile agent crossed plan boundary: %+v", messages)
	}
	var count, otherCount int
	_ = a.db.QueryRow(`SELECT count(*) FROM expense_estimates WHERE user_id=?`, id).Scan(&count)
	_ = a.db.QueryRow(`SELECT count(*) FROM expense_estimates WHERE user_id=?`, otherID).Scan(&otherCount)
	if count != 2 || otherCount != 0 {
		t.Fatalf("category isolation failed: owner=%d other=%d", count, otherCount)
	}
	confirm := request(t, a, "POST", "/profile", url.Values{"csrf": {csrf}, "income": {"2500"}, "outcome": {"2000"}, "feature": {"大学生，家里承担房租，想攒旅行费"}, "confirm": {"yes"}}, cookie)
	if confirm.Code != http.StatusSeeOther {
		t.Fatalf("confirm: %d", confirm.Code)
	}
	page := request(t, a, "GET", "/ledger", nil, cookie).Body.String()
	if !strings.Contains(page, `value="900.00"`) || !strings.Contains(page, "未分类月开销") {
		t.Fatal("conversation categories were not prefilled or reconciled")
	}
	request(t, a, "POST", "/ledger/skip", url.Values{"csrf": {csrf}}, cookie)
	p, _ := a.getProfile(id)
	cash, err := a.effectiveCashflow(id, p)
	if err != nil || cash.Source != "conversation_categories" || cash.NeedsCents != 190_000 || cash.WantsCents != 10_000 {
		t.Fatalf("skipped ledger discarded partial categories: %+v %v", cash, err)
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

func TestOptionalLedgerSupportsStudentAndUnknownOutcome(t *testing.T) {
	a := testApp(t)
	installTestPlanAgent(t, a, "已提交方案。")
	studentCookie, studentCSRF, studentID := registerTestUser(t, a, "student_flow")
	unknownCookie, unknownCSRF, unknownID := registerTestUser(t, a, "unknown_outcome")
	for _, tc := range []struct {
		cookie  *http.Cookie
		csrf    string
		income  string
		outcome string
		feature string
	}{
		{studentCookie, studentCSRF, "2500", "2000", "大学生，靠生活费，希望攒应急钱"},
		{unknownCookie, unknownCSRF, "6000", "", "收入不固定，暂时不清楚开销"},
	} {
		w := request(t, a, "POST", "/profile", url.Values{"csrf": {tc.csrf}, "income": {tc.income}, "outcome": {tc.outcome}, "feature": {tc.feature}, "confirm": {"yes"}}, tc.cookie)
		if w.Code != http.StatusSeeOther || !strings.HasPrefix(w.Header().Get("Location"), "/ledger") {
			t.Fatalf("profile: %d %s", w.Code, w.Header().Get("Location"))
		}
		w = request(t, a, "POST", "/ledger/skip", url.Values{"csrf": {tc.csrf}}, tc.cookie)
		if w.Code != http.StatusSeeOther || !strings.HasPrefix(w.Header().Get("Location"), "/plan") {
			t.Fatalf("skip: %d %s", w.Code, w.Header().Get("Location"))
		}
		w = request(t, a, "POST", "/plan/generate", url.Values{"csrf": {tc.csrf}}, tc.cookie)
		if w.Code != http.StatusSeeOther {
			t.Fatalf("generate: %d %s", w.Code, w.Body.String())
		}
	}
	studentPlan, err := a.latestPlan(studentID)
	if err != nil || studentPlan == nil || studentPlan.Provisional || studentPlan.NeedsCents != 200000 || studentPlan.IncomeCents != 250000 {
		t.Fatalf("student plan: %+v %v", studentPlan, err)
	}
	unknownPlan, err := a.latestPlan(unknownID)
	if err != nil || unknownPlan == nil || !unknownPlan.Provisional || unknownPlan.InvestableCents != 0 {
		t.Fatalf("unknown plan: %+v %v", unknownPlan, err)
	}
	if page := request(t, a, "GET", "/plan", nil, unknownCookie).Body.String(); !strings.Contains(page, "待补月支出") || strings.Contains(page, "可配置资金怎么分") {
		t.Fatal("provisional page exposed allocation")
	}
}

func TestQuickExpenseCategoriesOverrideConversationEstimate(t *testing.T) {
	a := testApp(t)
	cookie, csrf, id := registerTestUser(t, a, "quick_expenses")
	request(t, a, "POST", "/profile", url.Values{"csrf": {csrf}, "income": {"8000"}, "outcome": {"5000"}, "feature": {"固定工资"}, "confirm": {"yes"}}, cookie)
	w := request(t, a, "POST", "/ledger/quick", url.Values{"csrf": {csrf}, "expense_住房": {"2500"}, "expense_餐饮": {"1200"}, "expense_医疗": {"300"}, "expense_娱乐": {"500"}}, cookie)
	if w.Code != http.StatusSeeOther || !strings.HasPrefix(w.Header().Get("Location"), "/plan") {
		t.Fatalf("quick entry: %d %s", w.Code, w.Header().Get("Location"))
	}
	p, _ := a.getProfile(id)
	c, err := a.effectiveCashflow(id, p)
	if err != nil || c.Source != "quick" || c.NeedsCents != 450000 || c.WantsCents != 50000 {
		t.Fatalf("cashflow: %+v %v", c, err)
	}
	if page := request(t, a, "GET", "/ledger", nil, cookie).Body.String(); !strings.Contains(page, "导入 CSV") || !strings.Contains(page, "分类月开销") {
		t.Fatal("simplified ledger missing")
	}
}

func TestConversationOutcomeIncludesDebtOnce(t *testing.T) {
	a := testApp(t)
	_, _, id := registerTestUser(t, a, "outcome_debt")
	p := profile{IncomeCents: 800000, OutcomeCents: 500000, OutcomeKnown: true, DebtCents: 100000}
	c, err := a.effectiveCashflow(id, p)
	if err != nil || c.NeedsCents != 400000 || c.DebtCents != 100000 {
		t.Fatalf("outcome double counted debt: %+v %v", c, err)
	}
}

func TestPlanNarrativeNeverUsesUnverifiedModelNumbers(t *testing.T) {
	a := testApp(t)
	cookie, csrf, id := registerTestUser(t, a, "narrative_guard")
	request(t, a, "POST", "/profile", url.Values{"csrf": {csrf}, "income": {"2500"}, "outcome": {"2000"}, "feature": {"大学生"}, "confirm": {"yes"}}, cookie)
	request(t, a, "POST", "/ledger/skip", url.Values{"csrf": {csrf}}, cookie)
	installTestPlanAgent(t, a, "本月只结余 ¥50.00，预备金目标 ¥1200.00。")
	w := request(t, a, "POST", "/plan/generate", url.Values{"csrf": {csrf}}, cookie)
	if w.Code != http.StatusSeeOther {
		t.Fatalf("generate: %d %s", w.Code, w.Body.String())
	}
	p, err := a.latestPlan(id)
	if err != nil || p == nil || !strings.Contains(p.Narrative, "¥500.00") || strings.Contains(p.Narrative, "¥50.00") {
		t.Fatalf("unverified narrative: %+v %v", p, err)
	}
}
