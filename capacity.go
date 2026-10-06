package main

import (
	"errors"
	"net"
	"net/http"
	"sync"
	"time"
)

// These caps keep the single VPS predictable and are intentionally easy to tune.
const (
	maxUsers               = 100
	maxTransactionsPerUser = 10_000
	authAttemptsPerWindow  = 20
)

var errLedgerCapacity = errors.New("ledger capacity reached")

type rateWindow struct {
	start time.Time
	count int
}

type rateLimiter struct {
	mu      sync.Mutex
	windows map[string]rateWindow
}

func newRateLimiter() *rateLimiter {
	return &rateLimiter{windows: make(map[string]rateWindow)}
}

func (l *rateLimiter) allow(key string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := time.Now()
	if len(l.windows) > 1000 {
		for k, v := range l.windows {
			if now.Sub(v.start) >= 10*time.Minute {
				delete(l.windows, k)
			}
		}
		if len(l.windows) > 1000 {
			for k := range l.windows {
				delete(l.windows, k)
				break
			}
		}
	}
	w := l.windows[key]
	if now.Sub(w.start) >= 10*time.Minute {
		w = rateWindow{start: now}
	}
	w.count++
	l.windows[key] = w
	return w.count <= authAttemptsPerWindow
}

func (a *app) allowAuth(w http.ResponseWriter, r *http.Request) bool {
	if a.authRate == nil {
		return true
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	if !a.authRate.allow(host) {
		http.Error(w, "尝试过于频繁，请稍后再试", http.StatusTooManyRequests)
		return false
	}
	return true
}
