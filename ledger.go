package main

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
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
	Transactions                       []transaction
	Categories                         []string
	Month                              string
	Category                           string
	TransactionCount                   int
	HasPlan                            bool
	Income, Expense, Net, Needs, Wants int64
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
	d := ledgerData{Transactions: items, Categories: categories, Month: month, Category: category}
	if err := a.db.QueryRow(`SELECT count(*) FROM transactions WHERE user_id=?`, currentUser(r).ID).Scan(&d.TransactionCount); err != nil {
		http.Error(w, "账本读取失败", 500)
		return
	}
	var planCount int
	if err := a.db.QueryRow(`SELECT count(*) FROM plans WHERE user_id=?`, currentUser(r).ID).Scan(&planCount); err != nil {
		http.Error(w, "方案读取失败", 500)
		return
	}
	d.HasPlan = planCount > 0
	for _, t := range items {
		if t.Direction == "income" {
			d.Income += t.AmountCents
		} else {
			d.Expense += t.AmountCents
			if essentialCategory(t.Category) {
				d.Needs += t.AmountCents
			} else {
				d.Wants += t.AmountCents
			}
		}
	}
	d.Net = d.Income - d.Expense
	a.render(w, r, "ledger.html", pageData{Title: "账单工作台", Active: "ledger", Payload: d})
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
	redirect(w, r, "/ledger", "已载入当前账号专属模拟账单，可继续生成方案")
}
