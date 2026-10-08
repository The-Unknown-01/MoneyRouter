package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestJobFeedbackSupportsFullPageAndFragment(t *testing.T) {
	a := &app{}
	for _, ajax := range []bool{false, true} {
		r := httptest.NewRequest("POST", "/agent/month", nil)
		if ajax {
			r.Header.Set("HX-Request", "true")
		}
		w := httptest.NewRecorder()
		a.jobFragment(w, r, "month-job", "month", "2026-10")
		if ajax {
			if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `hx-get="/jobs/month-job"`) {
				t.Fatalf("missing polling feedback: %d %s", w.Code, w.Body.String())
			}
		} else {
			if w.Code != http.StatusSeeOther || w.Header().Get("Location") != "/month?period=2026-10" {
				t.Fatalf("missing full page redirect: %d %s", w.Code, w.Header().Get("Location"))
			}
			if strings.Contains(w.Body.String(), "正在处理") {
				t.Fatal("ordinary navigation exposed fragment")
			}
		}
	}
}
