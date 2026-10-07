package main

import (
	"database/sql"
	"net/http"
	"strconv"
	"strings"
)

type profileData struct {
	Profile   profile
	Questions []message
}

func (a *app) getProfile(userID int64) (profile, error) {
	p := profile{HorizonMonths: 36, Experience: "none"}
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
	var known int
	err = a.db.QueryRow(`SELECT outcome_cents,outcome_known,feature,income_source FROM profile_facts WHERE user_id=?`, userID).Scan(&p.OutcomeCents, &known, &p.Feature, &p.IncomeSource)
	if err != nil && err != sql.ErrNoRows {
		return p, err
	}
	p.OutcomeKnown = known != 0
	return p, nil
}

func (a *app) saveProfileRecord(userID int64, p profile) error {
	tx, err := a.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
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
	_, err = tx.Exec(`INSERT INTO profiles(user_id,income_cents,stable_income,family_load,debt_cents,reserve_cents,horizon_months,max_loss_pct,experience,goal,confirmed,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET income_cents=excluded.income_cents,stable_income=excluded.stable_income,family_load=excluded.family_load,debt_cents=excluded.debt_cents,reserve_cents=excluded.reserve_cents,horizon_months=excluded.horizon_months,max_loss_pct=excluded.max_loss_pct,experience=excluded.experience,goal=excluded.goal,confirmed=excluded.confirmed,updated_at=excluded.updated_at`, userID, p.IncomeCents, stable, family, p.DebtCents, p.ReserveCents, p.HorizonMonths, p.MaxLossPct, p.Experience, p.Goal, confirmed, utcNow())
	if err != nil {
		return err
	}
	known := 0
	if p.OutcomeKnown {
		known = 1
	}
	_, err = tx.Exec(`INSERT INTO profile_facts(user_id,outcome_cents,outcome_known,feature,income_source) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET outcome_cents=excluded.outcome_cents,outcome_known=excluded.outcome_known,feature=excluded.feature,income_source=excluded.income_source`, userID, p.OutcomeCents, known, p.Feature, p.IncomeSource)
	if err != nil {
		return err
	}
	return tx.Commit()
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
	p, err := a.getProfile(currentUser(r).ID)
	if err != nil {
		http.Error(w, "画像读取失败", 500)
		return
	}
	income, err := parseMoney(r.FormValue("income"))
	if err != nil {
		fail(w, r, "/profile", "请填写有效月收入")
		return
	}
	if income <= 0 {
		fail(w, r, "/profile", "请填写有效月收入或生活费")
		return
	}
	p.IncomeCents = income
	if raw := strings.TrimSpace(r.FormValue("outcome")); raw != "" {
		p.OutcomeCents, err = parseMoney(raw)
		if err != nil {
			fail(w, r, "/profile", "月支出估计无效")
			return
		}
		p.OutcomeKnown = true
	} else if _, ok := r.PostForm["outcome"]; ok {
		p.OutcomeCents = 0
		p.OutcomeKnown = false
	}
	if _, ok := r.PostForm["feature"]; ok {
		p.Feature = strings.TrimSpace(r.FormValue("feature"))
		if len(p.Feature) <= 500 {
			p.Goal = p.Feature
		}
	}
	if len(p.Feature) > 1000 {
		fail(w, r, "/profile", "特点描述过长")
		return
	}
	if source := strings.TrimSpace(r.FormValue("income_source")); len(source) <= 100 {
		p.IncomeSource = source
	}
	if raw := r.FormValue("debt"); raw != "" {
		p.DebtCents, err = parseMoney(raw)
		if err != nil {
			fail(w, r, "/profile", "债务金额无效")
			return
		}
	}
	if raw := r.FormValue("reserve"); raw != "" {
		p.ReserveCents, err = parseMoney(raw)
		if err != nil {
			fail(w, r, "/profile", "预备金金额无效")
			return
		}
	}
	if raw := r.FormValue("horizon"); raw != "" {
		v, e := strconv.Atoi(raw)
		if e != nil || v < 1 || v > 600 {
			fail(w, r, "/profile", "期限无效")
			return
		}
		p.HorizonMonths = v
	}
	if raw := r.FormValue("loss"); raw != "" {
		v, e := strconv.Atoi(raw)
		if e != nil || v < 0 || v > 100 {
			fail(w, r, "/profile", "亏损承受值无效")
			return
		}
		p.MaxLossPct = v
	}
	if raw := r.FormValue("stable"); raw != "" {
		p.StableIncome = raw == "yes"
	}
	if raw := r.FormValue("family"); raw != "" {
		p.FamilyLoad = raw == "yes"
	}
	if raw := r.FormValue("experience"); raw == "none" || raw == "some" || raw == "experienced" {
		p.Experience = raw
	}
	if raw := strings.TrimSpace(r.FormValue("goal")); raw != "" && len(raw) <= 500 {
		p.Goal = raw
	}
	if p.Goal == "" {
		p.Goal = p.Feature
	}
	p.Confirmed = r.FormValue("confirm") == "yes"
	if err := a.saveProfileRecord(currentUser(r).ID, p); err != nil {
		http.Error(w, "画像保存失败", 500)
		return
	}
	if p.Confirmed {
		redirect(w, r, "/ledger", "三项信息已确认，可补充开销或直接跳过")
	} else {
		redirect(w, r, "/profile", "草稿已保存，请核对后确认")
	}
}
