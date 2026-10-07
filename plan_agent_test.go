package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestPlanAgentRevisesRejectedCandidate(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		messages, _ := body["messages"].([]any)
		lastTool := ""
		for _, raw := range messages {
			m, _ := raw.(map[string]any)
			if m["role"] == "tool" {
				lastTool, _ = m["content"].(string)
			}
		}
		if strings.Contains(lastTool, "accepted") {
			_ = json.NewEncoder(w).Encode(map[string]any{"id": "final", "choices": []any{map[string]any{"message": map[string]any{"role": "assistant", "content": "结束"}, "finish_reason": "stop"}}})
			return
		}
		growth := 50
		conservative := 50
		if strings.Contains(lastTool, "rejected") {
			growth, conservative = 0, 100
		}
		proposal := planProposal{ReserveKind: "none", ReserveReason: "当前流动资金充足", ConservativePct: conservative, GrowthPct: growth,
			RiskLabel: "低", RiskReason: "用户尚未表达亏损意愿", DecisionReason: "优先保留随时可用的资金"}
		arguments, _ := json.Marshal(proposal)
		call := map[string]any{"id": "candidate", "type": "function", "function": map[string]any{"name": "submit_plan", "arguments": string(arguments)}}
		_ = json.NewEncoder(w).Encode(map[string]any{"id": "proposal", "choices": []any{map[string]any{"message": map[string]any{"role": "assistant", "content": "", "tool_calls": []any{call}}, "finish_reason": "tool_calls"}}})
	}))
	defer server.Close()
	client := &deepseekClient{key: "test", base: server.URL, http: server.Client(), sem: make(chan struct{}, 1)}
	profile := profile{IncomeCents: 250_000, HorizonMonths: 36, MaxLossPct: 0, Feature: "大学生，家庭承担住宿"}
	cash := cashflow{Source: "ledger", NeedsCents: 150_000, WantsCents: 50_000}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	result, err := client.GeneratePersonalPlan(ctx, profile, cash, preparePlan(profile, cash, nil))
	if err != nil {
		t.Fatal(err)
	}
	if requests != 3 || result.Growth != 0 || result.Conservative != 50_000 {
		t.Fatalf("agent did not revise rejected candidate: calls=%d result=%+v", requests, result)
	}
}

func TestPlanAgentRetriesMalformedToolArguments(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		messages, _ := body["messages"].([]any)
		for _, raw := range messages {
			m, _ := raw.(map[string]any)
			if m["role"] == "tool" {
				_ = json.NewEncoder(w).Encode(map[string]any{"id": "retry_final", "choices": []any{map[string]any{"message": map[string]any{"role": "assistant", "content": "结束"}, "finish_reason": "stop"}}})
				return
			}
		}
		arguments := `{"reserve_target_yuan":`
		if requests > 1 {
			candidate := testProposal()
			candidate.ConservativePct = 100
			encoded, _ := json.Marshal(candidate)
			arguments = string(encoded)
		}
		call := map[string]any{"id": "retry_call", "type": "function", "function": map[string]any{"name": "submit_plan", "arguments": arguments}}
		_ = json.NewEncoder(w).Encode(map[string]any{"id": "retry_proposal", "choices": []any{map[string]any{"message": map[string]any{"role": "assistant", "tool_calls": []any{call}}, "finish_reason": "tool_calls"}}})
	}))
	defer server.Close()
	client := &deepseekClient{key: "test", base: server.URL, http: server.Client(), sem: make(chan struct{}, 1)}
	profile := profile{IncomeCents: 250_000, HorizonMonths: 36, Feature: "大学生，家庭承担住宿"}
	cash := cashflow{Source: "ledger", NeedsCents: 150_000, WantsCents: 50_000}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	result, err := client.GeneratePersonalPlan(ctx, profile, cash, preparePlan(profile, cash, nil))
	if err != nil || result.Conservative != 50_000 || requests != 3 {
		t.Fatalf("malformed tool call was not recovered: calls=%d result=%+v err=%v", requests, result, err)
	}
}
