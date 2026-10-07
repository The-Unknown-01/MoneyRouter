package main

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"
)

type transaction struct {
	ID          int64
	Date        string
	Direction   string
	AmountCents int64
	Category    string
	Description string
	Note        string
	Source      string
}

var categories = []string{"工资", "其他收入", "住房", "餐饮", "交通", "医疗", "教育", "还款", "娱乐", "购物", "其他"}

func validCategory(v string) bool {
	for _, c := range categories {
		if v == c {
			return true
		}
	}
	return false
}
func essentialCategory(c string) bool {
	return c == "住房" || c == "餐饮" || c == "交通" || c == "医疗" || c == "教育"
}
func validMonth(v string) bool { _, err := time.Parse("2006-01", v); return err == nil && len(v) == 7 }
func validDate(v string) bool {
	_, err := time.Parse("2006-01-02", v)
	return err == nil && len(v) == 10
}

func (a *app) listTransactions(userID int64, month, category string) ([]transaction, error) {
	query := `SELECT id,date,direction,amount_cents,category,description,note,source FROM transactions WHERE user_id=?`
	args := []any{userID}
	if month != "" {
		query += ` AND substr(date,1,7)=?`
		args = append(args, month)
	}
	if category != "" {
		query += ` AND category=?`
		args = append(args, category)
	}
	query += ` ORDER BY date DESC,id DESC LIMIT 500`
	rows, err := a.db.Query(query, args...)
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

func (a *app) summarizeCashflow(userID int64) (cashflow, error) {
	rows, err := a.db.Query(`SELECT substr(date,1,7),direction,category,sum(amount_cents),count(*) FROM transactions WHERE user_id=? AND date<=? GROUP BY substr(date,1,7),direction,category ORDER BY substr(date,1,7) DESC`, userID, businessNow().Format("2006-01-02"))
	if err != nil {
		return cashflow{}, err
	}
	defer rows.Close()
	monthly := map[string]*cashflow{}
	months := []string{}
	for rows.Next() {
		var m, direction, category string
		var amount int64
		var count int
		if err := rows.Scan(&m, &direction, &category, &amount, &count); err != nil {
			return cashflow{}, err
		}
		if _, ok := monthly[m]; !ok {
			monthly[m] = &cashflow{}
			months = append(months, m)
		}
		c := monthly[m]
		c.Count += count
		if direction == "income" {
			c.IncomeCents += amount
		} else if category == "还款" {
			c.DebtCents += amount
		} else if essentialCategory(category) {
			c.NeedsCents += amount
		} else {
			c.WantsCents += amount
		}
	}
	if err := rows.Err(); err != nil {
		return cashflow{}, err
	}
	if len(months) > 1 && months[0] == businessNow().Format("2006-01") {
		months = months[1:]
	}
	count := len(months)
	if count > 3 {
		count = 3
	}
	result := cashflow{Months: count}
	for _, m := range months[:count] {
		c := monthly[m]
		if c.IncomeCents > 0 && (result.MinIncomeCents == 0 || c.IncomeCents < result.MinIncomeCents) {
			result.MinIncomeCents = c.IncomeCents
		}
		result.Count += c.Count
		result.IncomeCents += c.IncomeCents
		result.NeedsCents += c.NeedsCents
		result.WantsCents += c.WantsCents
		result.DebtCents += c.DebtCents
	}
	if count > 0 {
		n := int64(count)
		result.IncomeCents /= n
		result.NeedsCents /= n
		result.WantsCents /= n
		result.DebtCents /= n
	}
	return result, nil
}

type ledgerData struct {
	Transactions      []transaction
	Categories        []string
	ExpenseCategories []string
	Estimates         map[string]int64
	Status            string
	ProfileOutcome    int64
	OutcomeKnown      bool
	EstimateTotal     int64
	Unclassified      int64
	EstimateExcess    int64
	Month             string
	Category          string
	TransactionCount  int
}

func (a *app) ledger(w http.ResponseWriter, r *http.Request) {
	month := r.URL.Query().Get("month")
	if month != "" && !validMonth(month) {
		month = ""
	}
	category := r.URL.Query().Get("category")
	if category != "" && !validCategory(category) {
		category = ""
	}
	items, err := a.listTransactions(currentUser(r).ID, month, category)
	if err != nil {
		http.Error(w, "账本读取失败", 500)
		return
	}
	p, err := a.getProfile(currentUser(r).ID)
	if err != nil {
		http.Error(w, "画像读取失败", 500)
		return
	}
	d := ledgerData{Transactions: items, Categories: categories, ExpenseCategories: expenseCategories, Estimates: map[string]int64{}, Month: month, Category: category, ProfileOutcome: p.OutcomeCents, OutcomeKnown: p.OutcomeKnown}
	d.Status, err = a.ledgerStatus(currentUser(r).ID)
	if err != nil {
		http.Error(w, "流程读取失败", 500)
		return
	}
	rows, err := a.db.Query(`SELECT category,amount_cents FROM expense_estimates WHERE user_id=?`, currentUser(r).ID)
	if err != nil {
		http.Error(w, "开销读取失败", 500)
		return
	}
	for rows.Next() {
		var category string
		var amount int64
		if err := rows.Scan(&category, &amount); err != nil {
			rows.Close()
			http.Error(w, "开销读取失败", 500)
			return
		}
		d.Estimates[category] = amount
		d.EstimateTotal += amount
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		http.Error(w, "开销读取失败", 500)
		return
	}
	rows.Close()
	if d.OutcomeKnown {
		d.Unclassified = max64(0, d.ProfileOutcome-d.EstimateTotal)
		d.EstimateExcess = max64(0, d.EstimateTotal-d.ProfileOutcome)
	}
	if err := a.db.QueryRow(`SELECT count(*) FROM transactions WHERE user_id=?`, currentUser(r).ID).Scan(&d.TransactionCount); err != nil {
		http.Error(w, "账本读取失败", 500)
		return
	}
	a.render(w, r, "ledger.html", pageData{Title: "月度开销", Active: "ledger", Payload: d})
}

var expenseCategories = []string{"住房", "餐饮", "交通", "医疗", "教育", "还款", "购物", "娱乐", "其他"}
var errInvalidAgentExpense = errors.New("分类开销输入无效")

func validExpenseCategory(category string) bool {
	for _, allowed := range expenseCategories {
		if category == allowed {
			return true
		}
	}
	return false
}

func (a *app) saveAgentExpenseEstimates(userID int64, items []monthlyExpenseFact) error {
	if len(items) == 0 || len(items) > len(expenseCategories) {
		return fmt.Errorf("%w：分类数量无效", errInvalidAgentExpense)
	}
	amounts := make(map[string]int64, len(items))
	for _, item := range items {
		if !validExpenseCategory(item.Category) || math.IsNaN(item.AmountYuan) || math.IsInf(item.AmountYuan, 0) || item.AmountYuan < 0 || item.AmountYuan > 1_000_000_000 {
			return fmt.Errorf("%w：类别 %q 或金额无效", errInvalidAgentExpense, item.Category)
		}
		amounts[item.Category] = int64(math.Round(item.AmountYuan * 100))
	}
	tx, err := a.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for category, cents := range amounts {
		if _, err := tx.Exec(`INSERT INTO expense_estimates(user_id,category,amount_cents) VALUES(?,?,?) ON CONFLICT(user_id,category) DO UPDATE SET amount_cents=excluded.amount_cents`, userID, category, cents); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (a *app) categoryEstimateCashflow(userID int64, p profile) (cashflow, error) {
	rows, err := a.db.Query(`SELECT category,amount_cents FROM expense_estimates WHERE user_id=?`, userID)
	if err != nil {
		return cashflow{}, err
	}
	defer rows.Close()
	c := cashflow{Months: 1}
	for rows.Next() {
		var category string
		var amount int64
		if err := rows.Scan(&category, &amount); err != nil {
			return cashflow{}, err
		}
		c.Count++
		switch {
		case category == "还款":
			c.DebtCents += amount
		case essentialCategory(category):
			c.NeedsCents += amount
		default:
			c.WantsCents += amount
		}
	}
	if err := rows.Err(); err != nil {
		return cashflow{}, err
	}
	if c.Count == 0 {
		return c, nil
	}
	known := c.NeedsCents + c.WantsCents + c.DebtCents
	missingDebt := max64(0, p.DebtCents-c.DebtCents)
	remaining := max64(0, p.OutcomeCents-known)
	c.DebtCents += missingDebt
	c.NeedsCents += max64(0, remaining-missingDebt)
	return c, nil
}

func (a *app) ledgerStatus(userID int64) (string, error) {
	var status string
	err := a.db.QueryRow(`SELECT ledger_status FROM profile_facts WHERE user_id=?`, userID).Scan(&status)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	return status, err
}

func (a *app) setLedgerStatus(userID int64, status string) error {
	_, err := a.db.Exec(`INSERT INTO profile_facts(user_id,ledger_status) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET ledger_status=excluded.ledger_status`, userID, status)
	return err
}

func (a *app) saveQuickExpenses(w http.ResponseWriter, r *http.Request) {
	values := make(map[string]int64, len(expenseCategories))
	var total int64
	for _, category := range expenseCategories {
		raw := strings.TrimSpace(r.FormValue("expense_" + category))
		if raw == "" {
			continue
		}
		amount, err := parseMoney(raw)
		if err != nil {
			fail(w, r, "/ledger", category+"金额无效")
			return
		}
		values[category] = amount
		total += amount
	}
	if total <= 0 || total > 100_000_000_000 {
		fail(w, r, "/ledger", "请至少填写一项有效月开销，或选择跳过")
		return
	}
	u := currentUser(r)
	tx, err := a.db.Begin()
	if err != nil {
		http.Error(w, "保存失败", 500)
		return
	}
	defer tx.Rollback()
	if _, err = tx.Exec(`DELETE FROM expense_estimates WHERE user_id=?`, u.ID); err != nil {
		http.Error(w, "保存失败", 500)
		return
	}
	for category, amount := range values {
		if amount == 0 {
			continue
		}
		if _, err = tx.Exec(`INSERT INTO expense_estimates(user_id,category,amount_cents) VALUES(?,?,?)`, u.ID, category, amount); err != nil {
			http.Error(w, "保存失败", 500)
			return
		}
	}
	if _, err = tx.Exec(`INSERT INTO profile_facts(user_id,ledger_status) VALUES(?,'quick') ON CONFLICT(user_id) DO UPDATE SET ledger_status='quick'`, u.ID); err != nil {
		http.Error(w, "保存失败", 500)
		return
	}
	if err = tx.Commit(); err != nil {
		http.Error(w, "保存失败", 500)
		return
	}
	redirect(w, r, "/plan", "分类月开销已保存")
}

func (a *app) skipLedger(w http.ResponseWriter, r *http.Request) {
	if err := a.setLedgerStatus(currentUser(r).ID, "skipped"); err != nil {
		http.Error(w, "流程保存失败", 500)
		return
	}
	redirect(w, r, "/plan", "已进入分析，将采用对话中的月支出与已记录分类")
}

func (a *app) effectiveCashflow(userID int64, p profile) (cashflow, error) {
	status, err := a.ledgerStatus(userID)
	if err != nil {
		return cashflow{}, err
	}
	if status == "quick" || status == "skipped" {
		c, err := a.categoryEstimateCashflow(userID, p)
		if err != nil {
			return cashflow{}, err
		}
		if c.Count > 0 {
			if status == "quick" {
				c.Source = "quick"
			} else {
				c.Source = "conversation_categories"
			}
			return c, nil
		}
	}
	if status != "skipped" {
		c, err := a.summarizeCashflow(userID)
		if err != nil {
			return c, err
		}
		if c.Count > 0 && c.NeedsCents+c.WantsCents+c.DebtCents > 0 {
			c.Source = "ledger"
			return c, nil
		}
	}
	if p.OutcomeKnown {
		return cashflow{Source: "conversation", NeedsCents: max64(0, p.OutcomeCents-p.DebtCents), DebtCents: p.DebtCents}, nil
	}
	return cashflow{Source: "unknown"}, nil
}

func (a *app) saveTransaction(w http.ResponseWriter, r *http.Request) {
	u := currentUser(r)
	date := r.FormValue("date")
	direction := r.FormValue("direction")
	category := r.FormValue("category")
	amount, err := parseMoney(r.FormValue("amount"))
	if !validDate(date) || (direction != "income" && direction != "expense") || err != nil || amount <= 0 || !validCategory(category) {
		fail(w, r, "/ledger", "请检查日期、金额、方向与分类")
		return
	}
	desc := strings.TrimSpace(r.FormValue("description"))
	note := strings.TrimSpace(r.FormValue("note"))
	if len(desc) > 200 || len(note) > 500 {
		fail(w, r, "/ledger", "描述或备注过长")
		return
	}
	id, _ := strconv.ParseInt(r.FormValue("id"), 10, 64)
	if id > 0 {
		res, err := a.db.Exec(`UPDATE transactions SET date=?,direction=?,amount_cents=?,category=?,description=?,note=?,fingerprint=NULL WHERE id=? AND user_id=?`, date, direction, amount, category, desc, note, id, u.ID)
		if err != nil {
			http.Error(w, "修改失败", 500)
			return
		}
		n, _ := res.RowsAffected()
		if n == 0 {
			http.NotFound(w, r)
			return
		}
		if err := a.setLedgerStatus(u.ID, "manual"); err != nil {
			http.Error(w, "流程保存失败", 500)
			return
		}
		redirect(w, r, "/ledger", "记录已更新")
		return
	}
	var count int
	if err := a.db.QueryRow(`SELECT count(*) FROM transactions WHERE user_id=?`, u.ID).Scan(&count); err != nil {
		http.Error(w, "账本读取失败", 500)
		return
	}
	if count >= maxTransactionsPerUser {
		fail(w, r, "/ledger", "账本已达到当前账号容量上限")
		return
	}
	_, err = a.db.Exec(`INSERT INTO transactions(user_id,date,direction,amount_cents,category,description,note,source,created_at) VALUES(?,?,?,?,?,?,?,?,?)`, u.ID, date, direction, amount, category, desc, note, "manual", utcNow())
	if err != nil {
		http.Error(w, "保存失败", 500)
		return
	}
	if err := a.setLedgerStatus(u.ID, "manual"); err != nil {
		http.Error(w, "流程保存失败", 500)
		return
	}
	redirect(w, r, "/ledger", "记录已保存")
}

func (a *app) deleteTransaction(w http.ResponseWriter, r *http.Request) {
	id, err := strconv.ParseInt(r.FormValue("id"), 10, 64)
	if err != nil || id <= 0 {
		http.Error(w, "ID 无效", 400)
		return
	}
	res, err := a.db.Exec(`DELETE FROM transactions WHERE id=? AND user_id=?`, id, currentUser(r).ID)
	if err != nil {
		http.Error(w, "删除失败", 500)
		return
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		http.NotFound(w, r)
		return
	}
	var remaining int
	if err := a.db.QueryRow(`SELECT count(*) FROM transactions WHERE user_id=?`, currentUser(r).ID).Scan(&remaining); err != nil {
		http.Error(w, "账本读取失败", 500)
		return
	}
	if remaining == 0 {
		_ = a.setLedgerStatus(currentUser(r).ID, "")
	}
	redirect(w, r, "/ledger", "记录已删除")
}

func fingerprint(t transaction) string {
	h := sha256.Sum256([]byte(fmt.Sprintf("%s|%s|%d|%s|%s", t.Date, t.Direction, t.AmountCents, t.Category, strings.ToLower(strings.TrimSpace(t.Description)))))
	return hex.EncodeToString(h[:])
}

func (a *app) insertImported(userID int64, items []transaction) (int, int, error) {
	tx, err := a.db.Begin()
	if err != nil {
		return 0, 0, err
	}
	defer tx.Rollback()
	var existing int
	if err := tx.QueryRow(`SELECT count(*) FROM transactions WHERE user_id=?`, userID).Scan(&existing); err != nil {
		return 0, 0, err
	}
	if existing+len(items) > maxTransactionsPerUser {
		return 0, 0, errLedgerCapacity
	}
	inserted, duplicates := 0, 0
	for _, t := range items {
		res, err := tx.Exec(`INSERT OR IGNORE INTO transactions(user_id,date,direction,amount_cents,category,description,note,source,fingerprint,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)`, userID, t.Date, t.Direction, t.AmountCents, t.Category, t.Description, t.Note, "csv", fingerprint(t), utcNow())
		if err != nil {
			return 0, 0, err
		}
		n, _ := res.RowsAffected()
		if n == 0 {
			duplicates++
		} else {
			inserted++
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, 0, err
	}
	return inserted, duplicates, nil
}

func (a *app) seedDemo(w http.ResponseWriter, r *http.Request) {
	u := currentUser(r)
	var count int
	if err := a.db.QueryRow(`SELECT count(*) FROM transactions WHERE user_id=?`, u.ID).Scan(&count); err != nil {
		http.Error(w, "读取失败", 500)
		return
	}
	if count > 0 {
		fail(w, r, "/", "请先清空当前账本再载入样例")
		return
	}
	items := []transaction{}
	for offset := 1; offset <= 2; offset++ {
		month := businessNow().AddDate(0, -offset, 0).Format("2006-01")
		entries := []struct {
			day, dir       string
			amount         int64
			category, desc string
		}{
			{"05", "income", 1200000, "工资", "月度工资"}, {"06", "expense", 300000, "住房", "房租"}, {"08", "expense", 150000, "餐饮", "日常餐饮"}, {"10", "expense", 50000, "交通", "通勤"}, {"13", "expense", 60000, "教育", "学习课程"}, {"16", "expense", 80000, "娱乐", "休闲活动"}, {"18", "expense", 40000, "购物", "休闲购物"}, {"21", "expense", 30000, "还款", "分期还款"},
		}
		for _, e := range entries {
			items = append(items, transaction{Date: month + "-" + e.day, Direction: e.dir, AmountCents: e.amount, Category: e.category, Description: e.desc})
		}
	}
	if _, _, err := a.insertImported(u.ID, items); err != nil {
		if errors.Is(err, errLedgerCapacity) {
			fail(w, r, "/", "账本容量已满")
			return
		}
		http.Error(w, "样例载入失败", 500)
		return
	}
	if err := a.setLedgerStatus(u.ID, "imported"); err != nil {
		http.Error(w, "流程保存失败", 500)
		return
	}
	redirect(w, r, "/ledger", "已载入当前账号专属模拟账单，可继续生成方案")
}
