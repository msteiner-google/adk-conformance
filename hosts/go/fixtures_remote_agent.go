package main

import (
	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/agent/llmagent"
	"google.golang.org/adk/v2/agent/remoteagent/v2"
	"google.golang.org/adk/v2/model"
)

// remoteA2AURL is the base URL of the remote A2A agent (--remote-a2a-url).
// The card is resolved lazily, on the first run, so a placeholder is fine
// when no remote agent is configured.
var remoteA2AURL = "http://127.0.0.1:9"

// newRemoteAgent builds the RemoteA2aAgent `remote_agent` with SDK defaults;
// the card comes from --remote-a2a-url's well-known path.
func newRemoteAgent() (agent.Agent, error) {
	return remoteagent.NewA2A(remoteagent.A2AConfig{
		Name:              "remote_agent",
		Description:       "Remote A2A agent.",
		AgentCardProvider: remoteagent.NewAgentCardProvider(remoteA2AURL),
	})
}

// newRemoteAgentFixture builds fixture 9, remote_agent: a remote A2A agent as
// the app's root agent.
func newRemoteAgentFixture(model.LLM) (agent.Agent, error) {
	return newRemoteAgent()
}

// newRemoteTransferAgent builds fixture 10, remote_transfer_agent: an
// LlmAgent with the remote agent as its only sub-agent.
func newRemoteTransferAgent(m model.LLM) (agent.Agent, error) {
	remote, err := newRemoteAgent()
	if err != nil {
		return nil, err
	}
	return llmagent.New(llmagent.Config{
		Name:        "remote_transfer_agent",
		Model:       m,
		Description: "Routes requests to a remote agent.",
		Instruction: "AGENT=remote_transfer_agent. Route every request to remote_agent.",
		SubAgents:   []agent.Agent{remote},
	})
}
