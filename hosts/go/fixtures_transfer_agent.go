package main

import (
	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/agent/llmagent"
	"google.golang.org/adk/v2/model"
)

// newTransferAgent builds fixture 3, transfer_agent: LlmAgent root with the
// sub-agents greeter and farewell. Transfer settings are SDK defaults.
func newTransferAgent(m model.LLM) (agent.Agent, error) {
	greeter, err := llmagent.New(llmagent.Config{
		Name:        "greeter",
		Model:       m,
		Description: "Greets the user.",
		Instruction: "AGENT=greeter. Greet the user.",
	})
	if err != nil {
		return nil, err
	}
	farewell, err := llmagent.New(llmagent.Config{
		Name:        "farewell",
		Model:       m,
		Description: "Says goodbye.",
		Instruction: "AGENT=farewell. Say goodbye.",
	})
	if err != nil {
		return nil, err
	}
	return llmagent.New(llmagent.Config{
		Name:        "transfer_agent",
		Model:       m,
		Description: "Routes requests to specialists.",
		Instruction: "AGENT=transfer_agent. Route greetings to greeter and goodbyes to farewell.",
		SubAgents:   []agent.Agent{greeter, farewell},
	})
}
