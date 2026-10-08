package main

import "time"

// The product uses China calendar dates regardless of the Japanese VPS timezone.
var businessZone = time.FixedZone("Asia/Shanghai", 8*60*60)

func businessNow() time.Time { return time.Now().In(businessZone) }

func previousPeriod(period string) string {
	t, err := time.Parse("2006-01", period)
	if err != nil {
		return ""
	}
	previous := t.AddDate(0, -1, 0).Format("2006-01")
	if !validMonth(previous) {
		return ""
	}
	return previous
}

func nextPeriod(period string) string {
	t, err := time.Parse("2006-01", period)
	if err != nil {
		return ""
	}
	next := t.AddDate(0, 1, 0).Format("2006-01")
	if !validMonth(next) {
		return ""
	}
	return next
}
