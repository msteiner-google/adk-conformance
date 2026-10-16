package main

import (
	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/agent/llmagent"
	"google.golang.org/adk/v2/model"
)

// newToolsAgent builds fixture 2, tools_agent: LlmAgent with the shared tool
// library (except exit_loop), in spec order.
func newToolsAgent(m model.LLM) (agent.Agent, error) {
	tools, err := newTools(
		newAddTool,
		newGetWeatherTool,
		newSetStateTool,
		newFailAlwaysTool,
		newRequestApprovalTool,
	)
	if err != nil {
		return nil, err
	}
	return llmagent.New(llmagent.Config{
		Name:        "tools_agent",
		Model:       m,
		Description: "Agent with the shared tool library.",
		Instruction: "AGENT=tools_agent. Use the tools to answer.",
		Tools:       tools,
	})
}
