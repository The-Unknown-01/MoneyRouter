package main

import (
	"bytes"
	"embed"
	"flag"
	"fmt"
	"html/template"
	"io/fs"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

//go:embed web/templates/* web/static/*
var webFiles embed.FS

type app struct {
	db       *database
	tpl      *template.Template
	deepseek *deepseekClient
	news     *newsClient
	sem      chan struct{}
	previews *previewStore
	authRate *rateLimiter
}

type pageData struct {
	Title   string
	User    *user
	Active  string
	Flash   string
	Error   string
	CSRF    string
	Payload any
}

func main() {
	addr := flag.String("addr", ":8080", "HTTP listen address")
	dataDir := flag.String("data", "data", "SQLite data directory")
	backup := flag.String("backup", "", "create a consistent SQLite backup at this new path and exit")
	flag.Parse()
	if err := os.MkdirAll(*dataDir, 0700); err != nil {
		log.Fatal(err)
	}
	db, err := openDatabase(filepath.Join(*dataDir, "finance.db"))
	if err != nil {
		log.Fatal(err)
	}
	defer db.Close()
	if *backup != "" {
		if err := db.backup(*backup); err != nil {
			log.Fatal(err)
		}
		log.Printf("SQLite backup created: %s", *backup)
		return
	}
	a := &app{db: db, deepseek: newDeepseekClient(), news: newNewsClient(), sem: make(chan struct{}, 2), previews: newPreviewStore(), authRate: newRateLimiter()}
	a.tpl = newTemplates()
	server := &http.Server{Addr: *addr, Handler: a.routes(), ReadHeaderTimeout: 10 * time.Second, ReadTimeout: 35 * time.Second, WriteTimeout: 65 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 1 << 20}
	log.Printf("finance listening on %s", *addr)
	log.Fatal(server.ListenAndServe())
}

func newTemplates() *template.Template {
	return template.Must(template.New("layout.html").Funcs(template.FuncMap{
		"money":      formatMoney,
		"moneyInput": func(v int64) string { return fmt.Sprintf("%d.%02d", v/100, v%100) },
		"percent":    func(v int) string { return fmt.Sprintf("%d%%", v) },
		"date": func(v string) string {
			if len(v) >= 10 {
				return v[:10]
			}
			return v
		},
		"today":    func() string { return businessNow().Format("2006-01-02") },
		"monthNow": func() string { return businessNow().Format("2006-01") },
		"eq":       func(a, b any) bool { return a == b },
		"safeNL":   func(v string) template.HTML { return template.HTML(template.HTMLEscapeString(v)) },
	}).ParseFS(webFiles, "web/templates/*.html"))
}

func (a *app) routes() http.Handler {
	mux := http.NewServeMux()
	staticFS, err := fs.Sub(webFiles, "web/static")
	if err != nil {
		log.Fatal(err)
	}
	mux.Handle("GET /static/", http.StripPrefix("/static/", http.FileServer(http.FS(staticFS))))
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("GET /login", a.loginPage)
	mux.HandleFunc("POST /login", a.login)
	mux.HandleFunc("GET /register", a.registerPage)
	mux.HandleFunc("POST /register", a.register)
	mux.HandleFunc("POST /logout", a.withUser(a.logout))
	mux.HandleFunc("GET /", a.withUser(a.startPage))
	mux.HandleFunc("GET /ledger", a.withUser(a.requireStage("ledger", a.ledger)))
	mux.HandleFunc("POST /ledger", a.withUser(a.requireStage("ledger", a.saveTransaction)))
	mux.HandleFunc("POST /ledger/delete", a.withUser(a.requireStage("ledger", a.deleteTransaction)))
	mux.HandleFunc("POST /ledger/import", a.withUser(a.requireStage("ledger", a.importCSV)))
	mux.HandleFunc("GET /ledger/preview", a.withUser(a.requireStage("ledger", a.previewCSV)))
	mux.HandleFunc("POST /ledger/preview", a.withUser(a.requireStage("ledger", a.previewCSV)))
	mux.HandleFunc("POST /ledger/confirm", a.withUser(a.requireStage("ledger", a.confirmCSV)))
	mux.HandleFunc("POST /demo", a.withUser(a.requireStage("ledger", a.seedDemo)))
	mux.HandleFunc("GET /profile", a.withUser(a.profilePage))
	mux.HandleFunc("POST /profile", a.withUser(a.saveProfile))
	mux.HandleFunc("POST /profile/ask", a.withUser(a.askProfile))
	mux.HandleFunc("GET /plan", a.withUser(a.requireStage("plan", a.planPage)))
	mux.HandleFunc("POST /plan/generate", a.withUser(a.requireStage("plan", a.generatePlan)))
	mux.HandleFunc("POST /plan/adjust", a.withUser(a.requireStage("plan", a.adjustPlan)))
	mux.HandleFunc("GET /review", a.withUser(a.requireStage("optional", a.reviewPage)))
	mux.HandleFunc("GET /chat", a.withUser(a.requireStage("optional", a.chatPage)))
	mux.HandleFunc("POST /chat", a.withUser(a.requireStage("optional", a.chat)))
	mux.HandleFunc("POST /clear", a.withUser(a.clearData))
	return a.securityHeaders(mux)
}

func (a *app) render(w http.ResponseWriter, r *http.Request, name string, data pageData) {
	if data.User == nil {
		data.User = currentUser(r)
	}
	if data.Flash == "" {
		data.Flash = r.URL.Query().Get("ok")
	}
	if data.Error == "" {
		data.Error = r.URL.Query().Get("error")
	}
	if data.CSRF == "" && data.User != nil {
		data.CSRF = data.User.CSRF
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	var page bytes.Buffer
	if err := a.tpl.ExecuteTemplate(&page, name, data); err != nil {
		log.Printf("render %s: %v", name, err)
		http.Error(w, "页面渲染失败", http.StatusInternalServerError)
		return
	}
	_, _ = page.WriteTo(w)
}

func (a *app) securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		w.Header().Set("Referrer-Policy", "same-origin")
		w.Header().Set("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'")
		if r.Method == http.MethodPost && !sameOrigin(r) {
			http.Error(w, "请求来源无效", http.StatusForbidden)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func sameOrigin(r *http.Request) bool {
	origin := r.Header.Get("Origin")
	if origin == "" {
		return true
	}
	return strings.EqualFold(origin, "http://"+r.Host) || strings.EqualFold(origin, "https://"+r.Host)
}

type contextKey string

const userContextKey contextKey = "user"

func currentUser(r *http.Request) *user { u, _ := r.Context().Value(userContextKey).(*user); return u }

func redirect(w http.ResponseWriter, r *http.Request, path, message string) {
	if message != "" {
		sep := "?"
		if strings.Contains(path, "?") {
			sep = "&"
		}
		path += sep + "ok=" + urlQueryEscape(message)
	}
	http.Redirect(w, r, path, http.StatusSeeOther)
}

func fail(w http.ResponseWriter, r *http.Request, path, message string) {
	sep := "?"
	if strings.Contains(path, "?") {
		sep = "&"
	}
	http.Redirect(w, r, path+sep+"error="+urlQueryEscape(message), http.StatusSeeOther)
}
