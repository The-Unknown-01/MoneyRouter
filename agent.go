package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

type deepseekClient struct {
	key, base string
	http      *http.Client
	sem       chan struct{}
}
type aiMessage struct {
	Role       string       `json:"role"`
	Content    string       `json:"content"`
	ToolCallID string       `json:"tool_call_id,omitempty"`
	ToolCalls  []aiToolCall `json:"tool_calls,omitempty"`
}
type aiToolCall struct {
	ID       string `json:"id"`
	Type     string `json:"type"`
	Function struct {
		Name      string `json:"name"`
		Arguments string `json:"arguments"`
	} `json:"function"`
}
type aiChoice struct {
	FinishReason string    `json:"finish_reason"`
	Message      aiMessage `json:"message"`
}
type aiResponse struct {
	Choices []aiChoice `json:"choices"`
	Error   *struct {
		Message string `json:"message"`
	} `json:"error"`
}

func newDeepseekClient() *deepseekClient {
	key := strings.TrimSpace(os.Getenv("DEEPSEEK_API_KEY"))
	if key == "" {
		path := os.Getenv("DEEPSEEK_API_KEY_FILE")
		if path == "" {
			path = ".env/deepseek_api.key"
		}
		if b, err := os.ReadFile(path); err == nil {
			key = strings.TrimSpace(string(b))
		}
	}
	base := os.Getenv("DEEPSEEK_BASE_URL")
	if base == "" {
		base = "https://api.deepseek.com"
	}
	return &deepseekClient{key: key, base: strings.TrimRight(base, "/"), http: &http.Client{Timeout: 18 * time.Second}, sem: make(chan struct{}, 2)}
}
func (c *deepseekClient) Available() bool { return c.key != "" }

func (c *deepseekClient) call(ctx context.Context, messages []aiMessage, tools []map[string]any, jsonOutput bool) (aiMessage, error) {
	if !c.Available() {
		return aiMessage{}, errors.New("DeepSeek API key unavailable")
	}
	select {
	case c.sem <- struct{}{}:
		defer func() { <-c.sem }()
	case <-ctx.Done():
		return aiMessage{}, ctx.Err()
	}
	body := map[string]any{"model": "deepseek-flash", "messages": messages, "thinking": map[string]string{"type": "disabled"}, "max_tokens": 900}
	if len(tools) > 0 {
		body["tools"] = tools
		body["tool_choice"] = "auto"
	}
	if jsonOutput {
		body["response_format"] = map[string]string{"type": "json_object"}
	}
	encoded, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.base+"/chat/completions", bytes.NewReader(encoded))
	if err != nil {
		return aiMessage{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+c.key)
	resp, err := c.http.Do(req)
	if err != nil {
		return aiMessage{}, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return aiMessage{}, err
	}
	var decoded aiResponse
	if err := json.Unmarshal(b, &decoded); err != nil {
		return aiMessage{}, fmt.Errorf("invalid DeepSeek response: %w", err)
	}
	if resp.StatusCode != 200 {
		if decoded.Error != nil {
			return aiMessage{}, fmt.Errorf("DeepSeek %d: %s", resp.StatusCode, decoded.Error.Message)
		}
		return aiMessage{}, fmt.Errorf("DeepSeek HTTP %d", resp.StatusCode)
	}
	if len(decoded.Choices) == 0 {
		return aiMessage{}, errors.New("empty DeepSeek response")
	}
	return decoded.Choices[0].Message, nil
}

func (c *deepseekClient) Text(ctx context.Context, system, user string) (string, error) {
	msg, err := c.call(ctx, []aiMessage{{Role: "system", Content: system}, {Role: "user", Content: user}}, nil, false)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(msg.Content), nil
}

func (c *deepseekClient) JSON(ctx context.Context, system, user string) (string, error) {
	msg, err := c.call(ctx, []aiMessage{{Role: "system", Content: system}, {Role: "user", Content: user}}, nil, true)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(msg.Content), nil
}

func workflowTools() []map[string]any {
	names := []string{"summarize_cashflow", "allocate_budget", "calculate_reserve", "assess_risk", "propose_allocation", "find_sources", "check_goal"}
	desc := []string{"Get verified monthly cashflow from user ledger", "Get reproducible 50/30/20 comparison and budget allocation", "Get 3 or 6 month emergency reserve target and gap", "Get loss ability, willingness, horizon and growth cap", "Get constrained conservative, steady and growth amounts", "Get dated financial news sources for context only", "Get illustrative goal scenario, never a guaranteed forecast"}
	tools := make([]map[string]any, 0, len(names))
	for i, name := range names {
		tools = append(tools, map[string]any{"type": "function", "function": map[string]any{"name": name, "description": desc[i], "parameters": map[string]any{"type": "object", "properties": map[string]any{}, "additionalProperties": false}}})
	}
	return tools
}

func toolOutput(p plan, name string) (string, bool) {
	var value any
	switch name {
	case "summarize_cashflow":
		value = map[string]any{"income_cents": p.IncomeCents, "needs_cents": p.NeedsCents, "debt_cents": p.DebtCents, "data_quality": p.DataQuality}
	case "allocate_budget":
		value = map[string]any{"wants_cents": p.WantsCents, "savings_cents": p.SavingsCents, "reference": "50/30/20 of net income; actual needs and debt take precedence"}
	case "calculate_reserve":
		value = map[string]any{"months": p.ReserveMonths, "target_cents": p.ReserveTarget, "gap_cents": p.ReserveGap, "monthly_cents": p.ReserveMonthly}
	case "assess_risk":
		value = map[string]any{"risk": p.Risk, "reason": p.RiskReason, "growth_cap_pct": p.GrowthCapPct}
	case "propose_allocation":
		value = map[string]any{"conservative_cents": p.Conservative, "steady_cents": p.Steady, "growth_cents": p.Growth, "investable_cents": p.InvestableCents}
	case "find_sources":
		value = p.Sources
	case "check_goal":
		value = map[string]any{"target": ">3%", "illustrative_net_pct": p.ScenarioPct, "scenario_meets_target": p.GoalAchievable, "notice": "hypothetical scenario, not prediction or guarantee"}
	default:
		return "", false
	}
	b, _ := json.Marshal(value)
	return string(b), true
}

func (c *deepseekClient) ExplainPlan(ctx context.Context, p *plan, goal string) (string, error) {
	if !c.Available() {
		return "", errors.New("DeepSeek API key unavailable")
	}
	messages := []aiMessage{
		{Role: "system", Content: "你是中文个人财务规划 Agent。请在生成方案解释前调用专业计算工具，按现金流、预算、预备金、风险、配置、来源和目标检查顺序核对。所有金额只引用工具返回值，不得编造新闻或收益保证。最终用 4-6 句话解释方案与主要风险。"},
		{Role: "user", Content: fmt.Sprintf("请为用户生成并解释已由工具约束的预算与资产类别方案。用户目标：%s。你必须先调用相关工具；资讯只作背景。", goal)},
	}
	tools := workflowTools()
	called := map[string]bool{}
	for round := 0; round < 8; round++ {
		msg, err := c.call(ctx, messages, tools, false)
		if err != nil {
			return "", err
		}
		messages = append(messages, msg)
		if len(msg.ToolCalls) == 0 {
			for _, required := range []string{"summarize_cashflow", "allocate_budget", "calculate_reserve", "assess_risk", "propose_allocation", "check_goal"} {
				if !called[required] {
					return "", fmt.Errorf("Agent 未完成必要计算步骤：%s", required)
				}
			}
			return strings.TrimSpace(msg.Content), nil
		}
		for _, call := range msg.ToolCalls {
			output, ok := toolOutput(*p, call.Function.Name)
			if !ok {
				output = `{"error":"unknown tool"}`
			} else {
				called[call.Function.Name] = true
				p.Trace = append(p.Trace, traceStep{Tool: "Agent → " + call.Function.Name, Detail: "已读取可复算工具结果", Version: algorithmVersion})
			}
			messages = append(messages, aiMessage{Role: "tool", ToolCallID: call.ID, Content: output})
		}
	}
	return "", errors.New("Agent workflow exceeded step limit")
}

type newsItem struct {
	Title  string `json:"title"`
	URL    string `json:"url"`
	Date   string `json:"date"`
	Domain string `json:"domain"`
}
type newsClient struct {
	http    *http.Client
	mu      sync.Mutex
	cached  []newsItem
	expires time.Time
}

func newNewsClient() *newsClient { return &newsClient{http: &http.Client{Timeout: 5 * time.Second}} }

func (n *newsClient) Latest(ctx context.Context) []newsItem {
	n.mu.Lock()
	if time.Now().Before(n.expires) {
		items := append([]newsItem(nil), n.cached...)
		n.mu.Unlock()
		return items
	}
	n.mu.Unlock()
	url := "https://api.gdeltproject.org/api/v2/doc/doc?query=(economy%20OR%20monetary%20policy)&mode=artlist&maxrecords=8&timespan=3d&sort=datedesc&format=json"
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil
	}
	resp, err := n.http.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil
	}
	var decoded struct {
		Articles []struct {
			Title    string `json:"title"`
			URL      string `json:"url"`
			SeenDate string `json:"seendate"`
			Domain   string `json:"domain"`
		} `json:"articles"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&decoded); err != nil {
		return nil
	}
	items := []newsItem{}
	for _, v := range decoded.Articles {
		if !strings.HasPrefix(v.URL, "https://") || v.Title == "" {
			continue
		}
		date := v.SeenDate
		if len(date) >= 8 {
			date = date[:8]
		}
		items = append(items, newsItem{Title: v.Title, URL: v.URL, Date: date, Domain: v.Domain})
		if len(items) >= 5 {
			break
		}
	}
	n.mu.Lock()
	n.cached = items
	n.expires = time.Now().Add(30 * time.Minute)
	n.mu.Unlock()
	return items
}
