package main

import (
	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/agent/llmagent"
	"google.golang.org/adk/v2/model"
	"google.golang.org/adk/v2/tool"
	"google.golang.org/adk/v2/tool/agenttool"
)

// newAgentToolAgent builds fixture 7, agent_tool_agent: LlmAgent whose only
// tool is an AgentTool (SDK default settings) wrapping helper.
func newAgentToolAgent(m model.LLM) (agent.Agent, error) {
	helper, err := llmagent.New(llmagent.Config{
		Name:        "helper",
		Model:       m,
		Description: "Answers questions for the parent.",
		Instruction: "AGENT=helper. Answer the question.",
	})
	if err != nil {
		return nil, err
	}
	return llmagent.New(llmagent.Config{
		Name:        "agent_tool_agent",
		Model:       m,
		Description: "Delegates to a helper agent used as a tool.",
		Instruction: "AGENT=agent_tool_agent. Ask the helper tool.",
		Tools:       []tool.Tool{agenttool.New(helper, nil)},
	})
}
