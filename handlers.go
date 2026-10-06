package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"
)

type dashboardData struct {
	Profile          profile
	Cashflow         cashflow
	Plan             *plan
	TransactionCount int
}
type planData struct {
	Plan     *plan
	Profile  profile
	Cashflow cashflow
}
type reviewData struct {
	Plan                                                             *plan
	Month                                                            string
	ActualIncome, ActualExpense, ActualSaved, PlannedSaved, Variance int64
	ByCategory                                                       []categoryTotal
	HasData                                                          bool
}
type categoryTotal struct {
	Category string
	Amount   int64
}
type message struct {
	Role      string
	Content   string
	CreatedAt string
}
type chatData struct {
	Messages []message
	Plan     *plan
}

func (a *app) latestPlan(userID int64) (*plan, error) {
	var raw, narrative string
	err := a.db.QueryRow(`SELECT data_json,narrative FROM plans WHERE user_id=? ORDER BY version DESC LIMIT 1`, userID).Scan(&raw, &narrative)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var p plan
	if err := json.Unmarshal([]byte(raw), &p); err != nil {
		return nil, err
	}
	p.Narrative = narrative
	return &p, nil
}

func (a *app) dashboard(w http.ResponseWriter, r *http.Request) {
	u := currentUser(r)
	p, _ := a.getProfile(u.ID)
	cash, _ := a.summarizeCashflow(u.ID)
	latest, _ := a.latestPlan(u.ID)
	a.render(w, r, "dashboard.html", pageData{Title: "财务总览", Active: "home", Payload: dashboardData{Profile: p, Cashflow: cash, Plan: latest, TransactionCount: cash.Count}})
}

func (a *app) planPage(w http.ResponseWriter, r *http.Request) {
	u := currentUser(r)
	p, _ := a.getProfile(u.ID)
	cash, _ := a.summarizeCashflow(u.ID)
	latest, err := a.latestPlan(u.ID)
	if err != nil {
		http.Error(w, "方案读取失败", 500)
		return
	}
	a.render(w, r, "plan.html", pageData{Title: "我的方案", Active: "plan", Payload: planData{Plan: latest, Profile: p, Cashflow: cash}})
}

var errPlanVersionConflict = errors.New("plan version changed")

func (a *app) savePlan(userID int64, expectedVersion int, p *plan) error {
	tx, err := a.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var currentVersion int
	if err := tx.QueryRow(`SELECT coalesce(max(version),0) FROM plans WHERE user_id=?`, userID).Scan(&currentVersion); err != nil {
		return err
	}
	if currentVersion != expectedVersion {
		return errPlanVersionConflict
	}
	p.Version = currentVersion + 1
	data, err := json.Marshal(p)
	if err != nil {
		return err
	}
	trace, _ := json.Marshal(p.Trace)
	if _, err := tx.Exec(`INSERT INTO plans(user_id,version,data_json,narrative,trace_json,created_at) VALUES(?,?,?,?,?,?)`, userID, p.Version, string(data), p.Narrative, string(trace), utcNow()); err != nil {
		return err
	}
	if _, err := tx.Exec(`DELETE FROM plans WHERE user_id=? AND id NOT IN (SELECT id FROM plans WHERE user_id=? ORDER BY version DESC LIMIT 100)`, userID, userID); err != nil {
		return err
	}
	return tx.Commit()
}

func (a *app) generatePlan(w http.ResponseWriter, r *http.Request) {
	u := currentUser(r)
	profile, err := a.getProfile(u.ID)
	if err != nil {
		http.Error(w, "画像读取失败", 500)
		return
	}
	if !profile.Confirmed || profile.IncomeCents <= 0 {
		fail(w, r, "/profile", "请先填写并确认财务画像")
		return
	}
	cash, err := a.summarizeCashflow(u.ID)
	if err != nil {
		http.Error(w, "账单统计失败", 500)
		return
	}
	if cash.Count == 0 {
		fail(w, r, "/ledger", "请先录入账单或载入样例")
		return
	}
	previous, err := a.latestPlan(u.ID)
	if err != nil {
		http.Error(w, "方案读取失败", 500)
		return
	}
	expectedVersion := 0
	if previous != nil {
		expectedVersion = previous.Version
	}
	select {
	case a.sem <- struct{}{}:
		defer func() { <-a.sem }()
	default:
		fail(w, r, "/plan", "方案生成繁忙，请稍后重试")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 38*time.Second)
	defer cancel()
	sources := a.news.Latest(ctx)
	p := computePlan(profile, cash, sources)
	if err := validatePlan(p); err != nil {
		http.Error(w, "方案校验失败: "+err.Error(), 500)
		return
	}
	if a.deepseek.Available() {
		if narrative, err := a.deepseek.ExplainPlan(ctx, &p, profile.Goal); err == nil && narrative != "" {
			p.Narrative = narrative
		} else {
			p.Warnings = append(p.Warnings, "智能解释暂不可用，已使用可复算的规则说明")
		}
	}
	if p.Narrative == "" {
		p.Narrative = localNarrative(p)
	}
	if err := validatePlan(p); err != nil {
		http.Error(w, "方案复核失败: "+err.Error(), 500)
		return
	}
	if err := a.savePlan(u.ID, expectedVersion, &p); err != nil {
		if errors.Is(err, errPlanVersionConflict) {
			fail(w, r, "/plan", "方案已更新，请刷新后重试")
			return
		}
		http.Error(w, "方案保存失败", 500)
		return
	}
	redirect(w, r, "/plan", "已生成第 "+strconv.Itoa(p.Version)+" 版方案")
}

func localNarrative(p plan) string {
	return fmt.Sprintf("月收入 %s 中，先安排必要开支 %s、债务还款 %s 与可选开支 %s。本月计划结余 %s，其中 %s 补充预备金。风险档位为%s，增长类不超过可投资余额的 %d%%。示例年化情景 %.2f%% 仅用于比较，与实际收益无关。", formatMoney(p.IncomeCents), formatMoney(p.NeedsCents), formatMoney(p.DebtCents), formatMoney(p.WantsCents), formatMoney(p.SavingsCents), formatMoney(p.ReserveMonthly), p.Risk, p.GrowthCapPct, p.ScenarioPct)
}

func (a *app) adjustPlan(w http.ResponseWriter, r *http.Request) {
	u := currentUser(r)
	old, err := a.latestPlan(u.ID)
	if err != nil || old == nil {
		fail(w, r, "/plan", "请先生成方案")
		return
	}
	version, err := strconv.Atoi(r.FormValue("version"))
	if err != nil || version != old.Version {
		fail(w, r, "/plan", "方案已更新，请刷新后再调整")
		return
	}
	cap, err := parseMoney(r.FormValue("wants_cap"))
	if err != nil {
		fail(w, r, "/plan", "可选支出上限无效")
		return
	}
	profile, _ := a.getProfile(u.ID)
	cash, _ := a.summarizeCashflow(u.ID)
	if cap < cash.WantsCents {
		cash.WantsCents = cap
	}
	p := computePlan(profile, cash, old.Sources)
	p.Trace = append(p.Trace, traceStep{Tool: "user_adjustment", Detail: "用户设定可选支出上限 " + formatMoney(cap), Version: algorithmVersion})
	if err := validatePlan(p); err != nil {
		http.Error(w, "调整校验失败", 400)
		return
	}
	p.Narrative = localNarrative(p)
	if err := a.savePlan(u.ID, version, &p); err != nil {
		if errors.Is(err, errPlanVersionConflict) {
			fail(w, r, "/plan", "方案已更新，请刷新后重试")
			return
		}
		http.Error(w, "保存失败", 500)
		return
	}
	redirect(w, r, "/plan", "已生成调整后的第 "+strconv.Itoa(p.Version)+" 版方案")
}

func (a *app) reviewPage(w http.ResponseWriter, r *http.Request) {
	u := currentUser(r)
	latest, _ := a.latestPlan(u.ID)
	month := r.URL.Query().Get("month")
	if !validMonth(month) {
		month = businessNow().Format("2006-01")
	}
	items, err := a.listTransactions(u.ID, month, "")
	if err != nil {
		http.Error(w, "复盘读取失败", 500)
		return
	}
	d := reviewData{Plan: latest, Month: month, HasData: len(items) > 0}
	groups := map[string]int64{}
	for _, t := range items {
		if t.Direction == "income" {
			d.ActualIncome += t.AmountCents
		} else {
			d.ActualExpense += t.AmountCents
			groups[t.Category] += t.AmountCents
		}
	}
	for _, category := range categories {
		if groups[category] > 0 {
			d.ByCategory = append(d.ByCategory, categoryTotal{category, groups[category]})
		}
	}
	d.ActualSaved = d.ActualIncome - d.ActualExpense
	if latest != nil {
		d.PlannedSaved = latest.SavingsCents
		d.Variance = d.ActualSaved - latest.SavingsCents
	}
	a.render(w, r, "review.html", pageData{Title: "月末复盘", Active: "review", Payload: d})
}

func (a *app) addMessage(userID int64, scope, role, content string) error {
	_, err := a.db.Exec(`INSERT INTO messages(user_id,scope,role,content,created_at) VALUES(?,?,?,?,?)`, userID, scope, role, content, utcNow())
	if err != nil {
		return err
	}
	_, err = a.db.Exec(`DELETE FROM messages WHERE user_id=? AND scope=? AND id NOT IN (SELECT id FROM messages WHERE user_id=? AND scope=? ORDER BY id DESC LIMIT 200)`, userID, scope, userID, scope)
	return err
}

func (a *app) listMessages(userID int64, scope string, limit int) ([]message, error) {
	rows, err := a.db.Query(`SELECT role,content,created_at FROM messages WHERE user_id=? AND scope=? ORDER BY id DESC LIMIT ?`, userID, scope, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := []message{}
	for rows.Next() {
		var m message
		if err := rows.Scan(&m.Role, &m.Content, &m.CreatedAt); err != nil {
			return nil, err
		}
		items = append(items, m)
	}
	for i, j := 0, len(items)-1; i < j; i, j = i+1, j-1 {
		items[i], items[j] = items[j], items[i]
	}
	return items, rows.Err()
}

func (a *app) chatPage(w http.ResponseWriter, r *http.Request) {
	u := currentUser(r)
	messages, _ := a.listMessages(u.ID, "plan", 30)
	latest, _ := a.latestPlan(u.ID)
	a.render(w, r, "chat.html", pageData{Title: "方案答疑", Active: "chat", Payload: chatData{Messages: messages, Plan: latest}})
}

func (a *app) chat(w http.ResponseWriter, r *http.Request) {
	u := currentUser(r)
	input := strings.TrimSpace(r.FormValue("message"))
	if input == "" || len(input) > 2000 {
		fail(w, r, "/chat", "请输入不超过 2000 字的问题")
		return
	}
	latest, _ := a.latestPlan(u.ID)
	if latest == nil {
		fail(w, r, "/plan", "请先生成方案")
		return
	}
	_ = a.addMessage(u.ID, "plan", "user", input)
	answer := "请查看方案中的金额、风险上限和 workflow 步骤。如需调整可选支出，请使用方案页的调整表单。"
	if a.deepseek.Available() {
		ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
		defer cancel()
		prompt := fmt.Sprintf("当前用户方案：%s。用户问题：%s。只依据方案字段和来源回答，不编造收益或可用数据。若用户想改变预算，说明需在方案页调整并重新校验。", latest.JSON(), input)
		if response, err := a.deepseek.Text(ctx, "你是清晰、审慎的财务方案解释助手。", prompt); err == nil && response != "" {
			answer = response
		}
	}
	_ = a.addMessage(u.ID, "plan", "assistant", answer)
	redirect(w, r, "/chat", "")
}
