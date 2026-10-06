package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"

	"golang.org/x/crypto/bcrypt"
)

type user struct {
	ID       int64
	Username string
	CSRF     string
}

var usernamePattern = regexp.MustCompile(`^[a-zA-Z0-9_]{3,30}$`)

func randomHex(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

func tokenHash(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}
func urlQueryEscape(v string) string { return url.QueryEscape(v) }

func (a *app) withUser(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		cookie, err := r.Cookie("finance_session")
		if err != nil || cookie.Value == "" {
			http.Redirect(w, r, "/login", http.StatusSeeOther)
			return
		}
		var u user
		err = a.db.QueryRow(`SELECT u.id,u.username,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?`, tokenHash(cookie.Value), utcNow()).Scan(&u.ID, &u.Username, &u.CSRF)
		if err != nil {
			http.Redirect(w, r, "/login", http.StatusSeeOther)
			return
		}
		if r.Method == http.MethodPost {
			r.Body = http.MaxBytesReader(w, r.Body, 3<<20)
			var err error
			if strings.HasPrefix(r.Header.Get("Content-Type"), "multipart/form-data") {
				err = r.ParseMultipartForm(3 << 20)
			} else {
				err = r.ParseForm()
			}
			if err != nil {
				http.Error(w, "表单无效", http.StatusBadRequest)
				return
			}
			if r.FormValue("csrf") != u.CSRF {
				http.Error(w, "请求已失效，请刷新页面", http.StatusForbidden)
				return
			}
		}
		next(w, r.WithContext(context.WithValue(r.Context(), userContextKey, &u)))
	}
}

func (a *app) loginPage(w http.ResponseWriter, r *http.Request) {
	if a.loggedIn(r) {
		http.Redirect(w, r, "/", http.StatusSeeOther)
		return
	}
	a.render(w, r, "auth.html", pageData{Title: "登录", Active: "login", Payload: "login"})
}

func (a *app) registerPage(w http.ResponseWriter, r *http.Request) {
	if a.loggedIn(r) {
		http.Redirect(w, r, "/", http.StatusSeeOther)
		return
	}
	a.render(w, r, "auth.html", pageData{Title: "注册", Active: "register", Payload: "register"})
}

func (a *app) loggedIn(r *http.Request) bool {
	c, err := r.Cookie("finance_session")
	if err != nil {
		return false
	}
	var one int
	return a.db.QueryRow(`SELECT 1 FROM sessions WHERE token_hash=? AND expires_at>?`, tokenHash(c.Value), utcNow()).Scan(&one) == nil
}

func (a *app) register(w http.ResponseWriter, r *http.Request) {
	if !a.allowAuth(w, r) {
		return
	}
	if err := r.ParseForm(); err != nil {
		http.Error(w, "表单无效", http.StatusBadRequest)
		return
	}
	username := strings.TrimSpace(r.FormValue("username"))
	password := r.FormValue("password")
	if !usernamePattern.MatchString(username) {
		fail(w, r, "/register", "用户名需为 3-30 位字母、数字或下划线")
		return
	}
	if len(password) < 8 || len(password) > 72 {
		fail(w, r, "/register", "密码需为 8-72 位")
		return
	}
	var users int
	if err := a.db.QueryRow(`SELECT count(*) FROM users`).Scan(&users); err != nil {
		http.Error(w, "注册暂不可用", http.StatusInternalServerError)
		return
	}
	if users >= maxUsers {
		fail(w, r, "/register", "演示环境账号容量已满")
		return
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
	if err != nil {
		http.Error(w, "注册失败", http.StatusInternalServerError)
		return
	}
	res, err := a.db.Exec(`INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)`, username, string(hash), utcNow())
	if err != nil {
		fail(w, r, "/register", "用户名已存在或注册失败")
		return
	}
	id, _ := res.LastInsertId()
	if err := a.newSession(w, r, id); err != nil {
		http.Error(w, "会话创建失败", http.StatusInternalServerError)
		return
	}
	redirect(w, r, "/", "账号已创建，可以开始体验")
}

func (a *app) login(w http.ResponseWriter, r *http.Request) {
	if !a.allowAuth(w, r) {
		return
	}
	if err := r.ParseForm(); err != nil {
		http.Error(w, "表单无效", http.StatusBadRequest)
		return
	}
	username := strings.TrimSpace(r.FormValue("username"))
	var id int64
	var hash string
	err := a.db.QueryRow(`SELECT id,password_hash FROM users WHERE username=?`, username).Scan(&id, &hash)
	if errors.Is(err, sql.ErrNoRows) || (err == nil && bcrypt.CompareHashAndPassword([]byte(hash), []byte(r.FormValue("password"))) != nil) {
		fail(w, r, "/login", "用户名或密码错误")
		return
	}
	if err != nil {
		http.Error(w, "登录失败", http.StatusInternalServerError)
		return
	}
	if err := a.newSession(w, r, id); err != nil {
		http.Error(w, "会话创建失败", http.StatusInternalServerError)
		return
	}
	redirect(w, r, "/", "欢迎回来")
}

func (a *app) newSession(w http.ResponseWriter, r *http.Request, id int64) error {
	token, err := randomHex(32)
	if err != nil {
		return err
	}
	csrf, err := randomHex(16)
	if err != nil {
		return err
	}
	_, err = a.db.Exec(`INSERT INTO sessions(token_hash,user_id,csrf,expires_at) VALUES(?,?,?,?)`, tokenHash(token), id, csrf, time.Now().UTC().Add(7*24*time.Hour).Format(time.RFC3339Nano))
	if err != nil {
		return err
	}
	secure := r.TLS != nil || os.Getenv("COOKIE_SECURE") == "1"
	http.SetCookie(w, &http.Cookie{Name: "finance_session", Value: token, Path: "/", HttpOnly: true, Secure: secure, SameSite: http.SameSiteLaxMode, MaxAge: 7 * 24 * 3600})
	return nil
}

func (a *app) logout(w http.ResponseWriter, r *http.Request) {
	if c, err := r.Cookie("finance_session"); err == nil {
		_, _ = a.db.Exec(`DELETE FROM sessions WHERE token_hash=?`, tokenHash(c.Value))
	}
	http.SetCookie(w, &http.Cookie{Name: "finance_session", Path: "/", MaxAge: -1, HttpOnly: true})
	redirect(w, r, "/login", "已退出")
}

func (a *app) clearData(w http.ResponseWriter, r *http.Request) {
	if r.FormValue("confirm") != "CLEAR" {
		fail(w, r, "/profile", "请输入 CLEAR 确认清空")
		return
	}
	if err := a.db.clearUserData(currentUser(r).ID); err != nil {
		http.Error(w, "清空失败", http.StatusInternalServerError)
		return
	}
	redirect(w, r, "/", "当前账号的数据已清空")
}
