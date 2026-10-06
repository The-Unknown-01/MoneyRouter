package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"
)

type profileData struct {
	Profile    profile
	Questions  []message
	Categories []string
}

func (a *app) getProfile(userID int64) (profile, error) {
	p := profile{StableIncome: true, HorizonMonths: 36, Experience: "none"}
	var stable, family, confirmed int
	err := a.db.QueryRow(`SELECT income_cents,stable_income,family_load,debt_cents,reserve_cents,horizon_months,max_loss_pct,experience,goal,confirmed FROM profiles WHERE user_id=?`, userID).Scan(&p.IncomeCents, &stable, &family, &p.DebtCents, &p.ReserveCents, &p.HorizonMonths, &p.MaxLossPct, &p.Experience, &p.Goal, &confirmed)
	if err == sql.ErrNoRows {
		return p, nil
	}
	if err != nil {
		return p, err
	}
	p.StableIncome = stable != 0
	p.FamilyLoad = family != 0
	p.Confirmed = confirmed != 0
	return p, nil
}

func (a *app) saveProfileRecord(userID int64, p profile) error {
	stable, family, confirmed := 0, 0, 0
	if p.StableIncome {
		stable = 1
	}
	if p.FamilyLoad {
		family = 1
	}
	if p.Confirmed {
		confirmed = 1
	}
	_, err := a.db.Exec(`INSERT INTO profiles(user_id,income_cents,stable_income,family_load,debt_cents,reserve_cents,horizon_months,max_loss_pct,experience,goal,confirmed,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET income_cents=excluded.income_cents,stable_income=excluded.stable_income,family_load=excluded.family_load,debt_cents=excluded.debt_cents,reserve_cents=excluded.reserve_cents,horizon_months=excluded.horizon_months,max_loss_pct=excluded.max_loss_pct,experience=excluded.experience,goal=excluded.goal,confirmed=excluded.confirmed,updated_at=excluded.updated_at`, userID, p.IncomeCents, stable, family, p.DebtCents, p.ReserveCents, p.HorizonMonths, p.MaxLossPct, p.Experience, p.Goal, confirmed, utcNow())
	return err
}

func (a *app) profilePage(w http.ResponseWriter, r *http.Request) {
	p, err := a.getProfile(currentUser(r).ID)
	if err != nil {
		http.Error(w, "画像读取失败", 500)
		return
	}
	questions, _ := a.listMessages(currentUser(r).ID, "profile", 12)
	a.render(w, r, "profile.html", pageData{Title: "财务画像", Active: "profile", Payload: profileData{Profile: p, Questions: questions}})
}

func (a *app) saveProfile(w http.ResponseWriter, r *http.Request) {
	income, err := parseMoney(r.FormValue("income"))
	if err != nil {
		fail(w, r, "/profile", "请填写有效月收入")
		return
	}
	debt, err := parseMoney(defaultZero(r.FormValue("debt")))
	if err != nil {
		fail(w, r, "/profile", "债务还款金额无效")
		return
	}
	reserve, err := parseMoney(defaultZero(r.FormValue("reserve")))
	if err != nil {
		fail(w, r, "/profile", "预备金金额无效")
		return
	}
	horizon, _ := strconv.Atoi(r.FormValue("horizon"))
	loss, _ := strconv.Atoi(r.FormValue("loss"))
	if income <= 0 || horizon < 1 || horizon > 600 || loss < 0 || loss > 100 || debt > income*10 {
		fail(w, r, "/profile", "请检查收入、期限和亏损承受值")
		return
	}
	experience := r.FormValue("experience")
	if experience != "none" && experience != "some" && experience != "experienced" {
		experience = "none"
	}
	goal := strings.TrimSpace(r.FormValue("goal"))
	if len(goal) > 500 {
		fail(w, r, "/profile", "目标过长")
		return
	}
	p := profile{IncomeCents: income, StableIncome: r.FormValue("stable") == "yes", FamilyLoad: r.FormValue("family") == "yes", DebtCents: debt, ReserveCents: reserve, HorizonMonths: horizon, MaxLossPct: loss, Experience: experience, Goal: goal, Confirmed: r.FormValue("confirm") == "yes"}
	if err := a.saveProfileRecord(currentUser(r).ID, p); err != nil {
		http.Error(w, "画像保存失败", 500)
		return
	}
	if p.Confirmed {
		redirect(w, r, "/ledger", "画像已确认，下一步建立账本")
	} else {
		redirect(w, r, "/profile", "画像草稿已保存，请核对后确认")
	}
}

func defaultZero(v string) string {
	if strings.TrimSpace(v) == "" {
		return "0"
	}
	return v
}

func profileNextQuestion(p profile) string {
	if p.IncomeCents <= 0 {
		return "你的税后月收入通常是多少？收入是否稳定？"
	}
	if p.HorizonMonths < 1 {
		return "这笔钱预计多久之后需要使用？"
	}
	if p.MaxLossPct == 0 {
		return "如果短期出现亏损，你最多能接受亏损本金的百分之几？如果完全不能接受，请明确告诉我。"
	}
	if p.Goal == "" {
		return "你希望这份计划优先实现什么目标，例如应急储蓄、教育或长期积累？"
	}
	return "信息基本齐全。请检查右侧画像并勾选确认，然后生成方案。"
}

func (a *app) askProfile(w http.ResponseWriter, r *http.Request) {
	u := currentUser(r)
	input := strings.TrimSpace(r.FormValue("message"))
	if input == "" || len(input) > 2000 {
		fail(w, r, "/profile", "请输入不超过 2000 字的回答")
		return
	}
	p, _ := a.getProfile(u.ID)
	_ = a.addMessage(u.ID, "profile", "user", input)
	next := profileNextQuestion(p)
	reply := next
	if a.deepseek.Available() {
		ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
		defer cancel()
		prompt := fmt.Sprintf("当前画像：月税后收入=%.2f元，收入稳定=%t，家庭负担=%t，月债务=%.2f元，现有预备金=%.2f元，期限=%d月，最大可接受亏损=%d%%，投资经验=%s，目标=%s。用户新回答：%s。请从新回答中仅提取明确提到的字段，未提到的字段返回 null。输出 JSON 对象，字段为 income_yuan、stable_income、family_load、debt_yuan、reserve_yuan、horizon_months、max_loss_pct、experience（none/some/experienced）、goal、message、next_question。回复一句话并追问最关键的缺失信息；如已齐全，提醒用户核对表单并确认。建议下一问：%s", float64(p.IncomeCents)/100, p.StableIncome, p.FamilyLoad, float64(p.DebtCents)/100, float64(p.ReserveCents)/100, p.HorizonMonths, p.MaxLossPct, p.Experience, p.Goal, input, next)
		if response, err := a.deepseek.JSON(ctx, "你是财务画像信息收集 Agent。只能输出有效 JSON，不推测用户没有明说的金额和风险偏好。", prompt); err == nil && response != "" {
			var extracted struct {
				IncomeYuan    *float64 `json:"income_yuan"`
				StableIncome  *bool    `json:"stable_income"`
				FamilyLoad    *bool    `json:"family_load"`
				DebtYuan      *float64 `json:"debt_yuan"`
				ReserveYuan   *float64 `json:"reserve_yuan"`
				HorizonMonths *int     `json:"horizon_months"`
				MaxLossPct    *int     `json:"max_loss_pct"`
				Experience    *string  `json:"experience"`
				Goal          *string  `json:"goal"`
				Message       string   `json:"message"`
				NextQuestion  string   `json:"next_question"`
			}
			if json.Unmarshal([]byte(response), &extracted) == nil {
				changed := false
				if v := extracted.IncomeYuan; v != nil && *v > 0 && *v < 1_000_000_000 {
					p.IncomeCents = int64(math.Round(*v * 100))
					changed = true
				}
				if v := extracted.StableIncome; v != nil {
					p.StableIncome = *v
					changed = true
				}
				if v := extracted.FamilyLoad; v != nil {
					p.FamilyLoad = *v
					changed = true
				}
				if v := extracted.DebtYuan; v != nil && *v >= 0 && *v < 1_000_000_000 {
					p.DebtCents = int64(math.Round(*v * 100))
					changed = true
				}
				if v := extracted.ReserveYuan; v != nil && *v >= 0 && *v < 1_000_000_000 {
					p.ReserveCents = int64(math.Round(*v * 100))
					changed = true
				}
				if v := extracted.HorizonMonths; v != nil && *v >= 1 && *v <= 600 {
					p.HorizonMonths = *v
					changed = true
				}
				if v := extracted.MaxLossPct; v != nil && *v >= 0 && *v <= 100 {
					p.MaxLossPct = *v
					changed = true
				}
				if v := extracted.Experience; v != nil && (*v == "none" || *v == "some" || *v == "experienced") {
					p.Experience = *v
					changed = true
				}
				if v := extracted.Goal; v != nil && len(*v) <= 500 {
					p.Goal = *v
					changed = true
				}
				if changed {
					p.Confirmed = false
					_ = a.saveProfileRecord(u.ID, p)
				}
				reply = strings.TrimSpace(extracted.Message + " " + extracted.NextQuestion)
				if reply == "" {
					reply = profileNextQuestion(p)
				}
			}
		}
	}
	_ = a.addMessage(u.ID, "profile", "assistant", reply)
	redirect(w, r, "/profile", "已记录回答，请核对画像表单")
}
