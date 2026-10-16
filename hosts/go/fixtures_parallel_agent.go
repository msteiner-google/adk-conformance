package main

import (
	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/agent/llmagent"
	"google.golang.org/adk/v2/agent/workflowagents/parallelagent"
	"google.golang.org/adk/v2/model"
)

// newParallelAgent builds fixture 5, parallel_agent: ParallelAgent running
// left and right.
func newParallelAgent(m model.LLM) (agent.Agent, error) {
	left, err := llmagent.New(llmagent.Config{
		Name:        "left",
		Model:       m,
		Description: "Left branch.",
		Instruction: "AGENT=left. Answer briefly.",
	})
	if err != nil {
		return nil, err
	}
	right, err := llmagent.New(llmagent.Config{
		Name:        "right",
		Model:       m,
		Description: "Right branch.",
		Instruction: "AGENT=right. Answer briefly.",
	})
	if err != nil {
		return nil, err
	}
	return parallelagent.New(parallelagent.Config{
		AgentConfig: agent.Config{
			Name:        "parallel_agent",
			Description: "Runs two agents in parallel.",
			SubAgents:   []agent.Agent{left, right},
		},
	})
}
