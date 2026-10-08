package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"log"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"
)

type agentBridge struct {
	base, token string
	http        *http.Client
}

func newAgentBridge() *agentBridge {
	base := os.Getenv("AGENT_SERVICE_URL")
	if base == "" {
		base = "http://127.0.0.1:8090"
	}
	return &agentBridge{strings.TrimRight(base, "/"), os.Getenv("AGENT_SERVICE_TOKEN"), &http.Client{Timeout: 8 * time.Second}}
}
func (b *agentBridge) call(ctx context.Context, method, path string, input any, output any) error {
	var body io.Reader
	if input != nil {
		raw, err := json.Marshal(input)
		if err != nil {
			return err
		}
		body = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, b.base+path, body)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+b.token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := b.http.Do(req)
	if err != nil {
		return fmt.Errorf("agent unavailable: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("agent HTTP %d", resp.StatusCode)
	}
	if output == nil {
		return nil
	}
	decoder := json.NewDecoder(io.LimitReader(resp.Body, 16<<20))
	decoder.UseNumber()
	return decoder.Decode(output)
}
func agentUser(id int64) string { return fmt.Sprintf("u%d", id) }
func (a *app) agentState(ctx context.Context, id int64, period string) (map[string]any, error) {
	var state map[string]any
	err := a.bridge.call(ctx, "GET", "/v1/users/"+agentUser(id)+"/state?period="+url.QueryEscape(period), nil, &state)
	return state, err
}
func obj(v any) map[string]any {
	m, _ := v.(map[string]any)
	if m == nil {
		return map[string]any{}
	}
	return m
}
func arr(v any) []any { a, _ := v.([]any); return a }
func str(v any) string {
	if v == nil {
		return ""
	}
	return fmt.Sprint(v)
}
func num(v any) int64 { n, _ := strconv.ParseInt(str(v), 10, 64); return n }
func yes(v any) bool  { b, _ := v.(bool); return b }
func periodOf(r *http.Request) string {
	p := r.FormValue("period")
	if p == "" {
		p = businessNow().Format("2006-01")
	}
	return p
}
func (a *app) integratedNextStep(id int64) (string, error) {
	s, err := a.agentState(context.Background(), id, businessNow().Format("2006-01"))
	if err != nil {
		return "/profile", nil
	}
	if s["profile"] == nil {
		return "/profile", nil
	}
	if !yes(s["month_current"]) {
		return "/month", nil
	}
	return "/plan", nil
}
func (a *app) integratedRoutes() http.Handler {
	mux := http.NewServeMux()
	static, _ := fs.Sub(webFiles, "web/static")
	mux.Handle("GET /static/", http.StripPrefix("/static/", http.FileServer(http.FS(static))))
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) })
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) })
	readiness := func(w http.ResponseWriter, r *http.Request) {
		if a.bridge.call(r.Context(), "GET", "/readyz", nil, nil) != nil {
			http.Error(w, "Agent 服务未就绪", 503)
			return
		}
		w.Write([]byte("ok"))
	}
	mux.HandleFunc("GET /ready", readiness)
	mux.HandleFunc("GET /readyz", readiness)
	mux.HandleFunc("GET /login", a.loginPage)
	mux.HandleFunc("POST /login", a.login)
	mux.HandleFunc("GET /register", a.registerPage)
	mux.HandleFunc("POST /register", a.register)
	mux.HandleFunc("POST /logout", a.withUser(a.logout))
	mux.HandleFunc("GET /", a.withUser(a.startPage))
	for _, path := range []string{"/profile", "/ledger", "/month", "/plan", "/review", "/chat"} {
		mux.HandleFunc("GET "+path, a.withUser(a.integratedPage))
	}
	mux.HandleFunc("POST /agent/{kind}", a.withUser(a.agentSubmit))
	mux.HandleFunc("GET /jobs/{id}", a.withUser(a.agentPoll))
	mux.HandleFunc("POST /ledger", a.withUser(a.integratedSave))
	mux.HandleFunc("POST /ledger/delete", a.withUser(a.integratedDelete))
	mux.HandleFunc("POST /ledger/confirm", a.withUser(a.integratedImport))
	mux.HandleFunc("POST /clear", a.withUser(a.integratedClear))
	return a.securityHeaders(mux)
}

type uiField struct{ Key, Label, Value, Type string }
type uiStat struct{ Label, Value string }
type uiLine struct{ Label, Value string }
type webData struct {
	ReviewPlanVersion                                                                                                       string
	PeriodLabel, PeriodStatus, NextPeriod, ReviewMode                                                                       string
	PastPeriod, FuturePeriod, PlanReadOnly                                                                                  bool
	FinanceBriefing                                                                                                         map[string]any
	Title, Kind, Period, CSRF, RequestID, Error, Reply, JobID, JobKind, JobError, JobStatus, ChartJSON, Headline, Narrative string
	User                                                                                                                    *user
	State, Result                                                                                                           map[string]any
	Rows                                                                                                                    []transaction
	Categories, Warnings, Periods                                                                                           []string
	Fields                                                                                                                  []uiField
	WalletChanges                                                                                                           []uiStat
	Wallets                                                                                                                 []map[string]any
	ProfileConcepts                                                                                                         []uiStat
	Stats, Lines                                                                                                            []uiStat
	Messages                                                                                                                []message
	Awaiting, Confirmed, Stale, MonthCurrent, HasProfile, HasPlan, HasSummary                                               bool
}

var webTitles = map[string]string{"profile": "了解你", "ledger": "建立账本", "month": "核对本月实况", "plan": "你的方案", "review": "月度复盘", "chat": "方案答疑"}
var profileLabels = []uiField{
	{"outcome_cents", "通常月开销（元）", "", "money"}, {"feature", "生活情况与家庭支持", "", "text"}, {"occupation", "职业", "", "text"}, {"income_basis", "收入口径", "", "text"}, {"income_cents", "每月收入 / 生活费（元）", "", "money"}, {"income_stable", "收入是否稳定", "", "bool"}, {"family_load", "是否承担家庭负担", "", "bool"}, {"debt_cents", "负债总额（元）", "", "money"}, {"reserve_cents", "现有应急储蓄（元）", "", "money"}, {"horizon_months", "预计使用期限（月）", "", "number"}, {"max_loss_pct", "可承受亏损（%）", "", "number"}, {"experience", "投资经验", "", "experience"}, {"goal", "你的目标", "", "text"}, {"notes", "备注 · 补充信息", "", "text"},
}

func (a *app) integratedPage(w http.ResponseWriter, r *http.Request) {
	a.webPage(w, r, strings.TrimPrefix(r.URL.Path, "/"), "")
}
func (a *app) webPage(w http.ResponseWriter, r *http.Request, kind, jobID string) {
	period := periodOf(r)
	if !validMonth(period) {
		http.Error(w, "月份无效，请使用 YYYY-MM", http.StatusBadRequest)
		return
	}
	u := currentUser(r)
	s, err := a.agentState(r.Context(), u.ID, period)
	d := webData{Title: webTitles[kind], Kind: kind, Period: period, User: u, CSRF: u.CSRF, State: s, JobID: jobID}
	currentPeriod := businessNow().Format("2006-01")
	d.PastPeriod, d.FuturePeriod = period < currentPeriod, period > currentPeriod
	d.PlanReadOnly = kind == "plan" && d.PastPeriod
	d.NextPeriod = nextPeriod(period)
	d.PeriodLabel = map[string]string{"ledger": "账本月份", "month": "核对月份", "plan": "方案月份", "review": "复盘月份", "chat": "方案月份"}[kind]
	d.PeriodStatus = "进行中 · 实际数据截至资料截止日"
	if d.PastPeriod {
		d.PeriodStatus = "历史月份 · 整月资料仍需核对完整"
	}
	if d.FuturePeriod {
		d.PeriodStatus = "未来月份 · 预计资料与预案"
	}
	d.ReviewMode = str(s["review_mode"])
	if kind == "month" {
		d.Title = period + " 月度核对"
	}
	if kind == "plan" {
		d.Title = period + " 资金方案"
		if d.FuturePeriod {
			d.Title += " · 预案"
		}
		if d.PastPeriod {
			d.Title += " · 历史回看"
		}
	}
	if kind == "review" {
		d.Title = period + " 阶段回顾"
		if d.ReviewMode == "final" {
			d.Title = period + " 整月复盘"
		}
	}
	d.RequestID, _ = randomHex(16)
	if err != nil {
		log.Printf("agent state: %v", err)
		d.Error = "智能服务暂时不可用，请稍后重试。已有账单保留。"
	}
	d.HasProfile = s != nil && s["profile"] != nil
	d.MonthCurrent = yes(s["month_current"])
	d.Stale = yes(s["stale"])
	if err == nil && kind != "profile" && !d.HasProfile {
		a.webNavigate(w, r, "/profile")
		return
	}
	resultKind := kind
	if kind == "review" {
		resultKind = "summary"
	}
	d.Result = obj(s[resultKind+"_result"])
	d.Reply = str(d.Result["reply"])
	if notice := str(d.Result["service_notice"]); notice != "" {
		d.Warnings = append(d.Warnings, notice)
	}
	d.Awaiting = yes(d.Result["awaiting_confirmation"])
	d.Confirmed = yes(d.Result["confirmed"])
	if kind == "plan" && d.Stale {
		d.Awaiting = false
	}
	if kind == "month" && yes(s["month_draft_stale"]) {
		d.Awaiting, d.Confirmed = false, false
		d.Warnings = append(d.Warnings, "账本或画像已更新，请重新读取账本并核对。")
	}
	if kind == "month" && d.Confirmed && !d.MonthCurrent {
		d.Awaiting, d.Confirmed = false, false
		d.Warnings = append(d.Warnings, "该月资料需要按最新口径重新核对：实际已到账收入与预计整月收入分别填写，确认后再进入方案。")
	}
	if kind != "chat" {
		for _, v := range arr(d.Result["messages"]) {
			m := obj(v)
			d.Messages = append(d.Messages, message{Role: str(m["role"]), Content: str(m["content"])})
		}
	}
	if kind == "profile" {
		p := obj(d.Result["understanding"])
		if len(p) == 0 {
			p = obj(s["profile"])
		}
		d.ProfileConcepts = []uiStat{{"Income · 常规收入", displayCents(p["income_cents"])}, {"Outcome · 通常开销", displayCents(p["outcome_cents"])}, {"Feature · 生活与目标", strings.TrimSpace(str(p["feature"]) + " " + str(p["goal"]))}}
		for _, field := range profileLabels {
			v := p[field.Key]
			field.Value = str(v)
			if field.Type == "money" && v != nil {
				field.Value = fmt.Sprintf("%d.%02d", num(v)/100, num(v)%100)
			}
			d.Fields = append(d.Fields, field)
		}
	}
	for _, v := range arr(s["categories"]) {
		d.Categories = append(d.Categories, str(v))
	}
	d.Categories = append(d.Categories, "工资", "其他收入", "转账")
	for _, v := range arr(s["history_periods"]) {
		d.Periods = append(d.Periods, str(v))
	}
	if jobID == "" {
		pending := obj(s["pending"])
		if str(pending["kind"]) == resultKind || (kind == "ledger" && str(pending["kind"]) == "clean") {
			d.JobID = str(pending["job_id"])
			d.JobKind = str(pending["kind"])
		}
	}
	if d.JobID != "" {
		var j map[string]any
		if e := a.bridge.call(r.Context(), "GET", "/v1/users/"+agentUser(u.ID)+"/jobs/"+url.PathEscape(d.JobID), nil, &j); e == nil {
			d.JobStatus = str(j["status"])
			d.JobError = str(j["error"])
			if d.JobStatus == "succeeded" {
				d.JobID = ""
			}
		}
	}
	d.Rows, _ = a.listTransactions(u.ID, period, r.URL.Query().Get("category"))
	charts := map[string]any{}
	if kind == "ledger" {
		allItems, readErr := a.allMonthTransactions(u.ID, period)
		if readErr != nil {
			http.Error(w, "账本汇总读取失败", 500)
			return
		}
		var income, expense int64
		groups := map[string]int64{}
		for _, t := range allItems {
			if t.Direction == "income" {
				income += t.AmountCents
			} else if t.Direction == "expense" {
				expense += t.AmountCents
				groups[t.Category] += t.AmountCents
			}
		}
		d.Stats = []uiStat{{period + " 已记录收入", formatMoney(income)}, {period + " 已记录支出", formatMoney(expense)}, {"已记录净结余", formatMoney(income - expense)}}
		values := []any{}
		chartCategories := make([]string, 0, len(groups))
		for cat := range groups {
			chartCategories = append(chartCategories, cat)
		}
		sort.Strings(chartCategories)
		for _, cat := range chartCategories {
			if groups[cat] > 0 {
				values = append(values, map[string]any{"name": cat, "value": groups[cat]})
			}
		}
		charts["categories"] = values
	}
	if kind == "month" {
		if question := str(s["month_handoff"]); question != "" {
			d.Warnings = append(d.Warnings, "方案需要补充核对："+question)
		}
		snapshot := obj(d.Result["snapshot"])
		actualIncome := obj(snapshot["income"])["amount_cents"]
		if str(obj(snapshot["income"])["role"]) == "unknown" || (num(snapshot["schema_version"]) < 2 && str(obj(snapshot["income"])["source"]) == "stated") {
			actualIncome = nil
			snapshot["balance_cents"] = nil
			d.Warnings = append(d.Warnings, "旧记录的收入口径待核对，请分别确认已到账实际和预计整月总收入。")
		}
		d.Stats = []uiStat{{"已到账实际收入", displayCents(actualIncome)}, {"预计整月总收入", displayCents(obj(snapshot["expected_income"])["amount_cents"])}, {"截至资料截止日已花", displayCents(snapshot["spend_total_cents"])}, {"实际结余", displayCents(snapshot["balance_cents"])}}
		d.Narrative = str(d.Result["articulation"])
		for _, x := range arr(d.Result["parse_warnings"]) {
			d.Warnings = append(d.Warnings, str(x))
		}
		allocation := obj(snapshot["allocation"])
		d.Lines = append(d.Lines, uiStat{"保留储蓄", displayCents(allocation["non_invested_cents"])}, uiStat{"新增投资", displayCents(allocation["invested_cents"])})
	}
	if kind == "plan" {
		p := obj(d.Result["plan"])
		if len(p) == 0 {
			p = obj(s["confirmed_plan"])
			if len(p) > 0 {
				d.Warnings = append(d.Warnings, "新方案尚未完成，下方展示已有正式计划。")
			}
		}
		d.HasPlan = len(p) > 0
		if !d.HasPlan {
			d.Stale = false
		}
		version := r.URL.Query().Get("version")
		if version != "" {
			for _, v := range arr(s["versions"]) {
				if str(obj(v)["version"]) == version {
					p = obj(obj(v)["plan"])
					d.Awaiting = false
				}
			}
		}
		d.HasPlan = len(p) > 0
		budget := obj(p["budget"])
		d.FinanceBriefing = obj(obj(p["input_facts"])["briefing"])
		if len(d.FinanceBriefing) == 0 {
			d.FinanceBriefing = obj(obj(s["finance_result"])["briefing"])
		}
		reserve := obj(p["reserve"])
		d.Headline = str(p["headline"])
		d.Narrative = str(p["narrative"])
		d.Stats = []uiStat{{"每月储蓄", displayCents(budget["savings_cents"])}, {"应急金目标", displayCents(reserve["target_cents"])}, {"风险档位", str(obj(p["risk"])["level"])}}
		for _, key := range []string{"necessary_cents", "debt_cents", "wants_cents", "savings_cents"} {
			labels := map[string]string{"necessary_cents": "必要支出", "debt_cents": "债务还款", "wants_cents": "可选支出", "savings_cents": "储蓄"}
			d.Lines = append(d.Lines, uiStat{labels[key], displayCents(budget[key])})
		}
		charts["budget"] = budget
		charts["allocation"] = obj(p["allocation"])["recommended"]
		charts["reserve"] = reserve
		d.Wallets = nil
		for _, value := range arr(p["wallets"]) {
			d.Wallets = append(d.Wallets, obj(value))
		}
		charts["wallets"] = p["wallets"]
		previous := map[string]int64{}
		versions := arr(s["versions"])
		if len(versions) > 0 {
			last := obj(obj(versions[len(versions)-1])["plan"])
			for _, value := range arr(last["wallets"]) {
				row := obj(value)
				previous[str(row["id"])] = num(row["amount_cents"])
			}
			if !d.Awaiting && len(versions) > 1 {
				previous = map[string]int64{}
				for _, value := range arr(obj(obj(versions[len(versions)-2])["plan"])["wallets"]) {
					row := obj(value)
					previous[str(row["id"])] = num(row["amount_cents"])
				}
			}
		}
		for _, row := range d.Wallets {
			old, exists := previous[str(row["id"])]
			if exists && old != num(row["amount_cents"]) {
				d.WalletChanges = append(d.WalletChanges, uiStat{str(row["name"]), formatMoney(old) + " → " + displayCents(row["amount_cents"])})
			}
		}

		if num(p["schema_version"]) == 2 {
			d.Stats = []uiStat{{period + " 规划资金（含预计）", displayCents(obj(p["funding"])["total_cents"])}, {"消费预算", formatMoney(num(budget["necessary_cents"]) + num(budget["wants_cents"]) + num(budget["debt_cents"]))}, {"储蓄与投资", displayCents(budget["savings_cents"])}}
			d.Lines = nil
		}
		if str(d.Result["clarification_target"]) == "month" {
			d.Warnings = append(d.Warnings, "请在本月实况中补充助手提出的问题，确认后重新生成。")
		}
		for _, v := range arr(p["warnings"]) {
			d.Warnings = append(d.Warnings, str(v))
		}
		if !d.HasPlan {
			d.Stats, d.Lines = nil, nil
		}
	}
	if kind == "review" {
		summary := obj(d.Result["summary"])
		d.ReviewPlanVersion = str(obj(summary["diff"])["plan_version"])
		if d.ReviewPlanVersion == "" {
			d.ReviewPlanVersion = r.URL.Query().Get("plan_version")
		}
		if d.ReviewPlanVersion == "" && len(arr(s["versions"])) > 0 {
			d.ReviewPlanVersion = str(obj(arr(s["versions"])[0])["version"])
		}
		d.HasSummary = len(summary) > 0
		d.Headline = str(summary["headline"])
		d.Narrative = str(summary["narrative"])
		for _, section := range arr(summary["sections"]) {
			d.Narrative += str(section) + "\n"
		}
		charts["diff"] = obj(summary["diff"])["layers"]
		charts["history"] = s["history"]
		for _, v := range arr(summary["lessons"]) {
			d.Warnings = append(d.Warnings, str(v))
		}
	}
	if kind == "chat" {
		for _, v := range arr(d.Result["messages"]) {
			m := obj(v)
			d.Messages = append(d.Messages, message{Role: str(m["role"]), Content: str(m["content"])})
		}
	}
	// Import preview stays in Python's durable job result until explicit commit.
	preview := r.URL.Query().Get("preview")
	if preview != "" && kind == "ledger" {
		var j map[string]any
		if a.bridge.call(r.Context(), "GET", "/v1/users/"+agentUser(u.ID)+"/jobs/"+url.PathEscape(preview), nil, &j) == nil {
			d.Result = obj(j["result"])
			d.JobID = preview
			d.JobStatus = "preview"
			for _, x := range arr(d.Result["warnings"]) {
				d.Warnings = append(d.Warnings, str(x))
			}
		}
	}
	raw, _ := json.Marshal(charts)
	d.ChartJSON = string(raw)
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Add("Vary", "HX-Request")
	name := "web.html"
	if r.Header.Get("HX-Request") == "true" && r.Header.Get("HX-History-Restore-Request") != "true" {
		name = "web_content"
		w.Header().Set("HX-Push-Url", r.URL.RequestURI())
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	var out bytes.Buffer
	if e := a.tpl.ExecuteTemplate(&out, name, d); e != nil {
		log.Printf("web render: %v", e)
		http.Error(w, "页面暂时不可用", 500)
		return
	}
	w.Write(out.Bytes())
}
func displayCents(v any) string {
	if v == nil {
		return "尚未了解"
	}
	return formatMoney(num(v))
}
func (a *app) webNavigate(w http.ResponseWriter, r *http.Request, path string) {
	w.Header().Set("HX-Trigger", "operationCompleted")
	if r.Header.Get("HX-Request") == "true" {
		raw, _ := json.Marshal(map[string]string{"path": path, "target": "#app-main", "select": "#app-main", "swap": "outerHTML"})
		w.Header().Set("HX-Location", string(raw))
		w.WriteHeader(200)
	} else {
		http.Redirect(w, r, path, 303)
	}
}
func (a *app) webError(w http.ResponseWriter, r *http.Request, message string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(422)
	fmt.Fprintf(w, "<div class=\"inline-error\" role=\"alert\">%s</div>", templateEscape(message))
}
func templateEscape(v string) string {
	return strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;", "\"", "&quot;", "'", "&#39;").Replace(v)
}

func (a *app) agentSubmit(w http.ResponseWriter, r *http.Request) {
	kind := r.PathValue("kind")
	u := currentUser(r)
	period := periodOf(r)
	if !validMonth(period) {
		a.webError(w, r, "月份无效，请明确选择 YYYY-MM。")
		return
	}
	action := r.FormValue("action")
	payload := map[string]any{}
	if !strings.Contains("|profile|manual|month|finance|plan|summary|chat|clean|", "|"+kind+"|") {
		http.NotFound(w, r)
		return
	}
	s, err := a.agentState(r.Context(), u.ID, period)
	if err != nil {
		a.webError(w, r, "智能服务暂时不可用，输入已保留，请重试。")
		return
	}
	if kind != "profile" && kind != "manual" && s["profile"] == nil {
		a.webError(w, r, "请先确认画像。")
		return
	}
	if kind == "manual" {
		for _, field := range profileLabels {
			v := r.FormValue(field.Key)
			if v == "" {
				payload[field.Key] = nil
				continue
			}
			switch field.Type {
			case "money":
				n, e := parseMoney(v)
				if e != nil {
					a.webError(w, r, "请检查金额格式。")
					return
				}
				payload[field.Key] = n
			case "number":
				n, e := strconv.Atoi(v)
				if e != nil {
					a.webError(w, r, "请检查数字格式。")
					return
				}
				payload[field.Key] = n
			case "bool":
				payload[field.Key] = v == "true"
			default:
				payload[field.Key] = v
			}
		}
	}
	if kind == "month" && action == "" {
		items, e := a.allMonthTransactions(u.ID, period)
		if e != nil {
			a.webError(w, r, "本月账单读取失败，请重试。")
			return
		}
		doc := map[string]any{"period": period}
		var stored string
		if a.db.QueryRow("SELECT data FROM bill_documents WHERE user_id=? AND period=?", u.ID, period).Scan(&stored) == nil {
			json.Unmarshal([]byte(stored), &doc)
		}
		rows := []any{}
		for _, t := range items {
			rows = append(rows, map[string]any{"date": t.Date, "direction": t.Direction, "amount": fmt.Sprintf("%d.%02d", t.AmountCents/100, t.AmountCents%100), "category": t.Category, "note": strings.TrimSpace(t.Description + " " + t.Note)})
		}
		doc["cashflow"] = rows
		raw, _ := json.Marshal(doc)
		payload["bill"] = string(raw)
	}
	if kind == "plan" {
		if period < businessNow().Format("2006-01") {
			a.webError(w, r, "历史月份仅供查看与复盘，请选择当前或未来月份生成方案。")
			return
		}
		payload["revision"] = s["revision"]
		if !yes(s["month_current"]) {
			a.webError(w, r, "请先核对并确认最新的月度实况。")
			return
		}
	}
	if kind == "month" && action == "finish" {
		if r.FormValue("coverage_complete") == "true" {
			payload["coverage_complete"] = true
		}
		if r.FormValue("obligations_reviewed") == "true" {
			payload["obligations_reviewed"] = true
		}
		for _, key := range []string{"income_cents", "expected_income_cents", "non_invested_cents", "invested_cents"} {
			if value := r.FormValue(key); value != "" {
				n, err := parseMoney(value)
				if err != nil {
					a.webError(w, r, "请检查结余去向的金额。")
					return
				}
				payload[key] = n
			}
		}
		if value := r.FormValue("has_investments"); value != "" {
			payload["has_investments"] = value == "true"
		}
	}
	if kind == "summary" && r.FormValue("plan_version") != "" {
		payload["plan_version"] = r.FormValue("plan_version")
	}
	if kind == "clean" {
		f, h, e := r.FormFile("file")
		if e != nil {
			a.webError(w, r, "请选择账单文件。")
			return
		}
		defer f.Close()
		raw, e := io.ReadAll(io.LimitReader(f, (2<<20)+1))
		if e != nil || len(raw) > 2<<20 {
			a.webError(w, r, "文件不能超过 2 MiB。")
			return
		}
		payload = map[string]any{"content": base64.StdEncoding.EncodeToString(raw), "source": r.FormValue("source"), "filename": h.Filename}
	}
	id := r.FormValue("request_id")
	if id == "" {
		id, _ = randomHex(16)
	}
	command := map[string]any{"user_id": agentUser(u.ID), "kind": kind, "period": period, "request_id": id, "action": action, "user_message": r.FormValue("message"), "payload": payload}
	var job map[string]any
	if e := a.bridge.call(r.Context(), "POST", "/v1/jobs", command, &job); e != nil {
		log.Printf("submit: %v", e)
		a.webError(w, r, "任务提交失败，输入已保留，请重试。")
		return
	}
	jobID := str(job["job_id"])
	_, e := a.db.Exec("INSERT OR IGNORE INTO bridge_jobs(id,user_id,kind,period,created_at) VALUES(?,?,?,?,?)", jobID, u.ID, kind, period, utcNow())
	if e != nil {
		a.webError(w, r, "任务状态保存失败，请返回页面查看。")
		return
	}
	a.jobFragment(w, r, jobID, kind, period)
}
func (a *app) jobFragment(w http.ResponseWriter, r *http.Request, id, kind, period string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	fmt.Fprintf(w, `<div class="job-status" id="operation" hx-get="/jobs/%s" hx-trigger="every 2s" hx-target="this" hx-select="unset" hx-push-url="false" hx-swap="outerHTML" role="status"><span class="loading loading-spinner loading-sm"></span><span>正在处理，请稍候…</span><small>完成后会更新这里。你也可以离开，稍后回来查看。</small></div>`, templateEscape(id))
}
func kindPage(kind string) string {
	switch kind {
	case "manual":
		return "profile"
	case "clean":
		return "ledger"
	case "summary":
		return "review"
	default:
		return kind
	}
}
func (a *app) agentPoll(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var kind, period string
	var applied int
	if a.db.QueryRow("SELECT kind,period,applied FROM bridge_jobs WHERE id=? AND user_id=?", id, currentUser(r).ID).Scan(&kind, &period, &applied) != nil {
		http.NotFound(w, r)
		return
	}
	var j map[string]any
	if a.bridge.call(r.Context(), "GET", "/v1/users/"+agentUser(currentUser(r).ID)+"/jobs/"+url.PathEscape(id), nil, &j) != nil {
		a.webError(w, r, "暂时无法读取任务，刷新页面后可继续查看。")
		return
	}
	switch str(j["status"]) {
	case "queued", "running":
		a.jobFragment(w, r, id, kind, period)
	case "failed":
		w.Header().Set("HX-Trigger", "operationFailed")
		a.webError(w, r, str(j["error"]))
	default:
		path := "/" + kindPage(kind) + "?period=" + period
		if kind == "clean" {
			path += "&preview=" + id
		}
		a.webNavigate(w, r, path)
	}
}
func (a *app) allMonthTransactions(userID int64, period string) ([]transaction, error) {
	rows, err := a.db.Query("SELECT id,date,direction,amount_cents,category,description,note,source FROM transactions WHERE user_id=? AND substr(date,1,7)=? ORDER BY date,id", userID, period)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := []transaction{}
	for rows.Next() {
		var t transaction
		if err := rows.Scan(&t.ID, &t.Date, &t.Direction, &t.AmountCents, &t.Category, &t.Description, &t.Note, &t.Source); err != nil {
			return nil, err
		}
		items = append(items, t)
	}
	return items, rows.Err()
}
func (a *app) invalidate(ctx context.Context, id int64) error {
	return a.bridge.call(ctx, "POST", "/v1/users/"+agentUser(id)+"/invalidate", map[string]any{}, nil)
}
func (a *app) integratedSave(w http.ResponseWriter, r *http.Request) {
	u := currentUser(r)
	date, direction, category := r.FormValue("date"), r.FormValue("direction"), r.FormValue("category")
	amount, err := parseMoney(r.FormValue("amount"))
	if !validMonth(periodOf(r)) {
		a.webError(w, r, "月份无效。")
		return
	}
	if validDate(date) && date > businessNow().Format("2006-01-02") {
		a.webError(w, r, "未来费用请在月度核对中填写为待支付义务，不能记录为已发生流水。")
		return
	}
	s, e := a.agentState(r.Context(), u.ID, periodOf(r))
	valid := false
	for _, v := range arr(s["categories"]) {
		if str(v) == category {
			valid = true
		}
	}
	if direction != "expense" {
		valid = category == "工资" || category == "其他收入" || category == "转账"
	}
	if e != nil || s["profile"] == nil || !validDate(date) || err != nil || amount <= 0 || !valid || (direction != "income" && direction != "expense" && direction != "transfer") {
		a.webError(w, r, "请检查日期、金额、方向与分类，并先确认画像。")
		return
	}
	if len(r.FormValue("description")) > 200 || len(r.FormValue("note")) > 500 {
		a.webError(w, r, "描述或备注过长。")
		return
	}
	if a.invalidate(r.Context(), u.ID) != nil {
		a.webError(w, r, "服务暂时不可用，本次没有修改账单。")
		return
	}
	id, _ := strconv.ParseInt(r.FormValue("id"), 10, 64)
	if id > 0 {
		res, e := a.db.Exec("UPDATE transactions SET date=?,direction=?,amount_cents=?,category=?,description=?,note=?,fingerprint=NULL WHERE id=? AND user_id=?", date, direction, amount, category, r.FormValue("description"), r.FormValue("note"), id, u.ID)
		if e != nil {
			a.webError(w, r, "保存失败。")
			return
		}
		n, _ := res.RowsAffected()
		if n == 0 {
			http.NotFound(w, r)
			return
		}
	} else {
		var count int
		a.db.QueryRow("SELECT count(*) FROM transactions WHERE user_id=?", u.ID).Scan(&count)
		if count >= maxTransactionsPerUser {
			a.webError(w, r, "账本已达到容量上限。")
			return
		}
		_, e := a.db.Exec("INSERT INTO transactions(user_id,date,direction,amount_cents,category,description,note,source,created_at) VALUES(?,?,?,?,?,?,?,?,?)", u.ID, date, direction, amount, category, r.FormValue("description"), r.FormValue("note"), "manual", utcNow())
		if e != nil {
			a.webError(w, r, "保存失败。")
			return
		}
	}
	a.webNavigate(w, r, "/ledger?period="+date[:7])
}
func (a *app) integratedDelete(w http.ResponseWriter, r *http.Request) {
	if a.invalidate(r.Context(), currentUser(r).ID) != nil {
		a.webError(w, r, "服务暂时不可用，未删除。")
		return
	}
	res, e := a.db.Exec("DELETE FROM transactions WHERE id=? AND user_id=?", r.FormValue("id"), currentUser(r).ID)
	if e != nil {
		a.webError(w, r, "删除失败。")
		return
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		http.NotFound(w, r)
		return
	}
	a.webNavigate(w, r, "/ledger?period="+periodOf(r))
}
func (a *app) integratedImport(w http.ResponseWriter, r *http.Request) {
	u := currentUser(r)
	id := r.FormValue("job_id")
	var applied int
	if a.db.QueryRow("SELECT applied FROM bridge_jobs WHERE id=? AND user_id=? AND kind='clean'", id, u.ID).Scan(&applied) != nil {
		http.NotFound(w, r)
		return
	}
	if applied != 0 {
		a.webNavigate(w, r, "/ledger?period="+periodOf(r))
		return
	}
	var job map[string]any
	if a.bridge.call(r.Context(), "GET", "/v1/users/"+agentUser(u.ID)+"/jobs/"+url.PathEscape(id), nil, &job) != nil || str(job["status"]) != "succeeded" {
		a.webError(w, r, "预览尚未完成。")
		return
	}
	if a.invalidate(r.Context(), u.ID) != nil {
		a.webError(w, r, "服务暂时不可用，未导入。")
		return
	}
	tx, e := a.db.Begin()
	if e != nil {
		a.webError(w, r, "导入失败。")
		return
	}
	defer tx.Rollback()
	var count int
	tx.QueryRow("SELECT count(*) FROM transactions WHERE user_id=?", u.ID).Scan(&count)
	result := obj(job["result"])
	if count+len(arr(result["rows"])) > maxTransactionsPerUser {
		a.webError(w, r, "账本容量不足。")
		return
	}
	for _, v := range arr(result["rows"]) {
		t := obj(v)
		if !validDate(str(t["date"])) || str(t["date"]) > businessNow().Format("2006-01-02") {
			a.webError(w, r, "导入含无效或未来日期，请核对后重试；未来费用应作为待支付义务。")
			return
		}
		_, e = tx.Exec("INSERT OR IGNORE INTO transactions(user_id,date,direction,amount_cents,category,note,source,fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?)", u.ID, str(t["date"]), str(t["direction"]), num(t["amount_cents"]), str(t["category"]), str(t["note"]), str(t["source"]), str(t["fingerprint"]), utcNow())
		if e != nil {
			a.webError(w, r, "导入失败，请检查文件。")
			return
		}
	}
	for p, doc := range obj(result["documents"]) {
		raw, _ := json.Marshal(doc)
		if _, e = tx.Exec("INSERT OR REPLACE INTO bill_documents VALUES(?,?,?)", u.ID, p, string(raw)); e != nil {
			a.webError(w, r, "保存账单附加信息失败。")
			return
		}
	}
	tx.Exec("UPDATE bridge_jobs SET applied=1 WHERE id=?", id)
	if tx.Commit() != nil {
		a.webError(w, r, "导入保存失败。")
		return
	}
	a.webNavigate(w, r, "/ledger?period="+periodOf(r))
}
func (a *app) integratedClear(w http.ResponseWriter, r *http.Request) {
	if r.FormValue("confirm") != "CLEAR" {
		a.webError(w, r, "请输入 CLEAR 确认清空。")
		return
	}
	u := currentUser(r)
	if a.bridge.call(r.Context(), "POST", "/v1/users/"+agentUser(u.ID)+"/clear", map[string]any{}, nil) != nil {
		a.webError(w, r, "请等待任务完成，或稍后重试。")
		return
	}
	a.db.Exec("DELETE FROM bridge_jobs WHERE user_id=?", u.ID)
	a.db.Exec("DELETE FROM bill_documents WHERE user_id=?", u.ID)
	if a.db.clearUserData(u.ID) != nil {
		a.webError(w, r, "清空失败，请重试。")
		return
	}
	a.webNavigate(w, r, "/profile")
}
