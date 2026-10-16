package main

import (
	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/agent/llmagent"
	"google.golang.org/adk/v2/model"
)

// newHelloAgent builds fixture 1, hello_agent: LlmAgent, no tools.
func newHelloAgent(m model.LLM) (agent.Agent, error) {
	return llmagent.New(llmagent.Config{
		Name:        "hello_agent",
		Model:       m,
		Description: "Hello-world fixture agent.",
		Instruction: "You are a friendly assistant. Answer in one short sentence.",
	})
}
