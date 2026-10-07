package main

import (
	"net/http"
)

// nextStep is the only entry point after login. It preserves progress when a
// user returns and keeps the product journey linear without a dashboard.
func (a *app) nextStep(userID int64) (string, error) {
	p, err := a.getProfile(userID)
	if err != nil {
		return "", err
	}
	if !p.Confirmed || p.IncomeCents <= 0 {
		return "/profile", nil
	}
	status, err := a.ledgerStatus(userID)
	if err != nil {
		return "", err
	}
	if status == "skipped" || status == "quick" {
		return "/plan", nil
	}
	var count int
	if err := a.db.QueryRow(`SELECT count(*) FROM transactions WHERE user_id=?`, userID).Scan(&count); err != nil {
		return "", err
	}
	if count == 0 {
		return "/ledger", nil
	}
	return "/plan", nil
}

func (a *app) startPage(w http.ResponseWriter, r *http.Request) {
	path, err := a.nextStep(currentUser(r).ID)
	if err != nil {
		http.Error(w, "无法读取当前进度", http.StatusInternalServerError)
		return
	}
	http.Redirect(w, r, path, http.StatusSeeOther)
}

// requireStage applies to both reads and writes, so a direct URL cannot skip
// the profile or ledger step. Review and Q&A open only from a saved plan.
func (a *app) requireStage(stage string, next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		step, err := a.nextStep(currentUser(r).ID)
		if err != nil {
			http.Error(w, "无法读取当前进度", http.StatusInternalServerError)
			return
		}
		if step == "/profile" || (stage != "ledger" && step == "/ledger") {
			http.Redirect(w, r, step, http.StatusSeeOther)
			return
		}
		if stage == "optional" {
			plan, err := a.latestPlan(currentUser(r).ID)
			if err != nil {
				http.Error(w, "无法读取当前方案", http.StatusInternalServerError)
				return
			}
			if plan == nil {
				http.Redirect(w, r, "/plan", http.StatusSeeOther)
				return
			}
		}
		next(w, r)
	}
}
