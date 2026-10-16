package main

import (
	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/agent/llmagent"
	"google.golang.org/adk/v2/agent/workflowagents/loopagent"
	"google.golang.org/adk/v2/model"
)

// newLoopAgent builds fixture 6, loop_agent: LoopAgent (max_iterations 3)
// repeating looper, which has the built-in exit_loop tool.
func newLoopAgent(m model.LLM) (agent.Agent, error) {
	tools, err := newTools(newExitLoopTool)
	if err != nil {
		return nil, err
	}
	looper, err := llmagent.New(llmagent.Config{
		Name:        "looper",
		Model:       m,
		Description: "Does one unit of work.",
		Instruction: "AGENT=looper. Do one unit of work; call exit_loop when finished.",
		Tools:       tools,
	})
	if err != nil {
		return nil, err
	}
	return loopagent.New(loopagent.Config{
		AgentConfig: agent.Config{
			Name:        "loop_agent",
			Description: "Repeats a worker.",
			SubAgents:   []agent.Agent{looper},
		},
		MaxIterations: 3,
	})
}
