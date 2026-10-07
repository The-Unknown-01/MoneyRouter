package main

import (
	"context"
	"fmt"
	"log"
	"math"
	"net/http"
	"strings"
	"sync"
	"time"

	deepseekmodel "github.com/cloudwego/eino-ext/components/model/deepseek"
	"github.com/cloudwego/eino/adk"
	"github.com/cloudwego/eino/components/tool"
	"github.com/cloudwego/eino/components/tool/utils"
	"github.com/cloudwego/eino/compose"
	"github.com/cloudwego/eino/schema"
)

// The model chooses what to ask and when to record a fact. The tool only accepts
// explicit user statements; all monetary and risk bounds are checked in Go.
type profileFactsInput struct {
	IncomeYuan    *float64 `json:"income_yuan,omitempty" jsonschema_description:"Monthly available income or allowance in yuan, only if explicitly stated"`
	OutcomeYuan   *float64 `json:"outcome_yuan,omitempty" jsonschema_description:"Approximate total monthly spending in yuan, only if explicitly stated"`
	IncomeSource  *string  `json:"income_source,omitempty" jsonschema_description:"Salary, family allowance, scholarship or another stated source"`
	Feature       *string  `json:"feature,omitempty" jsonschema_description:"Short factual summary of life stage, obligations, goals and preferences stated by the user"`
	StableIncome  *bool    `json:"stable_income,omitempty" jsonschema_description:"Whether inflow is regular, only when clear"`
	FamilyLoad    *bool    `json:"family_load,omitempty" jsonschema_description:"Whether user supports dependents, only when clear"`
	DebtYuan      *float64 `json:"debt_yuan,omitempty" jsonschema_description:"Monthly debt payment in yuan, only if stated"`
	ReserveYuan   *float64 `json:"reserve_yuan,omitempty" jsonschema_description:"Existing accessible emergency cash in yuan, only if stated"`
	HorizonMonths *int     `json:"horizon_months,omitempty" jsonschema_description:"Investment horizon in months, only if stated"`
	MaxLossPct    *int     `json:"max_loss_pct,omitempty" jsonschema_description:"Maximum acceptable short-term principal loss percentage, only if stated"`
	Experience    *string  `json:"experience,omitempty" jsonschema_description:"Investment experience: none, some, experienced, only when clear"`
	Goal          *string  `json:"goal,omitempty" jsonschema_description:"Financial goal stated by user"`
}

func applyProfileFacts(p *profile, in profileFactsInput) bool {
	changed := false
	if v := in.IncomeYuan; v != nil && !math.IsNaN(*v) && *v > 0 && *v <= 1_000_000_000 {
		p.IncomeCents = int64(math.Round(*v * 100))
		changed = true
	}
	if v := in.OutcomeYuan; v != nil && !math.IsNaN(*v) && *v >= 0 && *v <= 1_000_000_000 {
		p.OutcomeCents = int64(math.Round(*v * 100))
		p.OutcomeKnown = true
		changed = true
	}
	if v := in.IncomeSource; v != nil && len(*v) <= 100 {
		p.IncomeSource = strings.TrimSpace(*v)
		changed = true
	}
	if v := in.Feature; v != nil && len(*v) <= 1000 {
		p.Feature = strings.TrimSpace(*v)
		changed = true
	}
	if v := in.StableIncome; v != nil {
		p.StableIncome = *v
		changed = true
	}
	if v := in.FamilyLoad; v != nil {
		p.FamilyLoad = *v
		changed = true
	}
	if v := in.DebtYuan; v != nil && !math.IsNaN(*v) && *v >= 0 && *v <= 1_000_000_000 {
		p.DebtCents = int64(math.Round(*v * 100))
		changed = true
	}
	if v := in.ReserveYuan; v != nil && !math.IsNaN(*v) && *v >= 0 && *v <= 1_000_000_000 {
		p.ReserveCents = int64(math.Round(*v * 100))
		changed = true
	}
	if v := in.HorizonMonths; v != nil && *v >= 1 && *v <= 600 {
		p.HorizonMonths = *v
		changed = true
	}
	if v := in.MaxLossPct; v != nil && *v >= 0 && *v <= 100 {
		p.MaxLossPct = *v
		changed = true
	}
	if v := in.Experience; v != nil && (*v == "none" || *v == "some" || *v == "experienced") {
		p.Experience = *v
		changed = true
	}
	if v := in.Goal; v != nil && len(*v) <= 500 {
		p.Goal = strings.TrimSpace(*v)
		changed = true
	}
	if changed {
		p.Confirmed = false
	}
	return changed
}

func (a *app) profileAgentReply(ctx context.Context, userID int64, p *profile, history []message, input string) (string, error) {
	if !a.deepseek.Available() {
		return "", fmt.Errorf("model unavailable")
	}
	select {
	case a.deepseek.sem <- struct{}{}:
		defer func() { <-a.deepseek.sem }()
	case <-ctx.Done():
		return "", ctx.Err()
	}
	model, err := deepseekmodel.NewChatModel(ctx, &deepseekmodel.ChatModelConfig{
		APIKey: a.deepseek.key, BaseURL: a.deepseek.base, Model: "deepseek-flash",
		Timeout: 20 * time.Second, MaxTokens: 650,
	})
	if err != nil {
		return "", err
	}
	var mu sync.Mutex
	record, err := utils.InferTool("record_profile_facts", "Record only facts explicitly stated by this user; omit unknown fields. Call whenever a message contains new or corrected facts.",
		func(_ context.Context, in *profileFactsInput) (string, error) {
			if in == nil {
				return "No facts supplied", nil
			}
			mu.Lock()
			defer mu.Unlock()
			if applyProfileFacts(p, *in) {
				if err := a.saveProfileRecord(userID, *p); err != nil {
					return "", err
				}
			}
			return fmt.Sprintf("Current confirmed draft: income %.2f yuan/month, outcome known %t, outcome %.2f yuan/month, feature %q. Continue naturally and ask only for missing material facts.", float64(p.IncomeCents)/100, p.OutcomeKnown, float64(p.OutcomeCents)/100, p.Feature), nil
		})
	if err != nil {
		return "", err
	}
	outcome := "unknown"
	if p.OutcomeKnown {
		outcome = fmt.Sprintf("%.2f yuan/month", float64(p.OutcomeCents)/100)
	}
	instruction := fmt.Sprintf(`你是中文财务画像对话 Agent。自由地与用户多轮交谈，目标只围绕 Income（月度可用收入或生活费）、Outcome（月度总支出，包含还款，可粗估）、Feature（个人特点、责任、目标与风险意愿）。不按固定题序询问。用户一次说全时直接总结；缺少关键内容时只问当前最有用的一个问题。允许学生、自由职业者等群体。用户明确说出的新增或修正事实必须先调用 record_profile_facts；不要猜测金额、风险意愿或投资期限。若 Outcome 缺失，温和地问一个粗略月支出；用户不知道时可告知将先给不含投资金额的临时建议。不要宣称收益保证。当前草稿：Income %.2f 元/月，来源 %q；Outcome %s；Feature %q；收入稳定 %t；月债务 %.2f 元；可用预备金 %.2f 元；可承受亏损 %d%%；目标 %q。回复简短自然，不要求填写旧版长表单。`, float64(p.IncomeCents)/100, p.IncomeSource, outcome, p.Feature, p.StableIncome, float64(p.DebtCents)/100, float64(p.ReserveCents)/100, p.MaxLossPct, p.Goal)
	agent, err := adk.NewChatModelAgent(ctx, &adk.ChatModelAgentConfig{
		Name: "profile_interviewer", Instruction: instruction, Model: model, MaxIterations: 3,
		ToolsConfig: adk.ToolsConfig{ToolsNodeConfig: compose.ToolsNodeConfig{Tools: []tool.BaseTool{record}}},
	})
	if err != nil {
		return "", err
	}
	messages := make([]*schema.Message, 0, len(history)+1)
	for _, m := range history {
		switch m.Role {
		case "user":
			messages = append(messages, schema.UserMessage(m.Content))
		case "assistant":
			messages = append(messages, schema.AssistantMessage(m.Content, nil))
		}
	}
	messages = append(messages, schema.UserMessage(input))
	iter := agent.Run(ctx, &adk.AgentInput{Messages: messages})
	reply := ""
	for {
		event, ok := iter.Next()
		if !ok {
			break
		}
		if event.Err != nil {
			return "", event.Err
		}
		if event.Output == nil || event.Output.MessageOutput == nil {
			continue
		}
		msg, err := event.Output.MessageOutput.GetMessage()
		if err != nil {
			return "", err
		}
		if msg != nil && msg.Role == schema.Assistant && strings.TrimSpace(msg.Content) != "" {
			reply = strings.TrimSpace(msg.Content)
		}
	}
	if reply == "" {
		return "", fmt.Errorf("empty agent response")
	}
	return reply, nil
}

func (a *app) askProfile(w http.ResponseWriter, r *http.Request) {
	u := currentUser(r)
	input := strings.TrimSpace(r.FormValue("message"))
	if input == "" || len(input) > 2000 {
		fail(w, r, "/profile", "请输入不超过 2000 字的回答")
		return
	}
	p, err := a.getProfile(u.ID)
	if err != nil {
		http.Error(w, "画像读取失败", 500)
		return
	}
	history, err := a.listMessages(u.ID, "profile", 12)
	if err != nil {
		http.Error(w, "对话读取失败", 500)
		return
	}
	if err := a.addMessage(u.ID, "profile", "user", input); err != nil {
		http.Error(w, "对话保存失败", 500)
		return
	}
	reply := "我在听。可以说说你每月大概有多少可用的钱、花多少钱，以及你现在最在意什么；不知道的数字可以先留空。"
	if a.deepseek.Available() {
		ctx, cancel := context.WithTimeout(r.Context(), 25*time.Second)
		defer cancel()
		if generated, err := a.profileAgentReply(ctx, u.ID, &p, history, input); err == nil {
			reply = generated
		} else {
			log.Printf("profile agent failed: %v", err)
			reply = "助手暂时无法整理这一轮回答。你可以重试，或在下方直接核对 Income、Outcome、Feature。"
		}
	}
	if err := a.addMessage(u.ID, "profile", "assistant", reply); err != nil {
		http.Error(w, "对话保存失败", 500)
		return
	}
	redirect(w, r, "/profile", "已更新对话与三项摘要")
}
