package main

import (
	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/agent/llmagent"
	"google.golang.org/adk/v2/model"
)

// newTemplateAgent builds fixture 8, template_agent: LlmAgent whose
// instruction reads {user_name} from session state.
func newTemplateAgent(m model.LLM) (agent.Agent, error) {
	return llmagent.New(llmagent.Config{
		Name:        "template_agent",
		Model:       m,
		Description: "Agent whose instruction reads session state.",
		Instruction: "AGENT=template_agent. The user's name is {user_name}.",
	})
}
