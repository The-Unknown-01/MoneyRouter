package main

import "time"

// The product uses China calendar dates regardless of the Japanese VPS timezone.
var businessZone = time.FixedZone("Asia/Shanghai", 8*60*60)

func businessNow() time.Time { return time.Now().In(businessZone) }
