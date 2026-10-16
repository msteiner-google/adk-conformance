package main

import (
	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/agent/llmagent"
	"google.golang.org/adk/v2/agent/workflowagents/sequentialagent"
	"google.golang.org/adk/v2/model"
)

// newSequentialAgent builds fixture 4, sequential_agent: SequentialAgent
// running step_one (output_key topic) then step_two (templates {topic}).
func newSequentialAgent(m model.LLM) (agent.Agent, error) {
	stepOne, err := llmagent.New(llmagent.Config{
		Name:        "step_one",
		Model:       m,
		Description: "Picks a topic.",
		Instruction: "AGENT=step_one. Pick a topic.",
		OutputKey:   "topic",
	})
	if err != nil {
		return nil, err
	}
	stepTwo, err := llmagent.New(llmagent.Config{
		Name:        "step_two",
		Model:       m,
		Description: "Writes about the topic.",
		Instruction: "AGENT=step_two. Write one sentence about {topic}.",
	})
	if err != nil {
		return nil, err
	}
	return sequentialagent.New(sequentialagent.Config{
		AgentConfig: agent.Config{
			Name:        "sequential_agent",
			Description: "Runs two steps in order.",
			SubAgents:   []agent.Agent{stepOne, stepTwo},
		},
	})
}
