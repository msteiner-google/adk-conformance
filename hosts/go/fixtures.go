package main

// Fixture registry. fixtures/FIXTURES.md is the canonical spec; each fixture
// lives in its own fixtures_<name>.go file.

import (
	"fmt"

	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/model"
)

// fixtureBuilder builds one fixture's root agent (app name == root agent
// name). Every call returns a fresh agent tree.
type fixtureBuilder func(m model.LLM) (agent.Agent, error)

// fixtureBuilders lists all fixtures in FIXTURES.md order.
var fixtureBuilders = []fixtureBuilder{
	newHelloAgent,
	newToolsAgent,
	newTransferAgent,
	newSequentialAgent,
	newParallelAgent,
	newLoopAgent,
	newAgentToolAgent,
	newTemplateAgent,
	newRemoteAgentFixture,
	newRemoteTransferAgent,
}

// buildFixture builds one fixture's root agent by app name.
func buildFixture(m model.LLM, app string) (agent.Agent, error) {
	roots, err := buildFixtures(m)
	if err != nil {
		return nil, err
	}
	for _, root := range roots {
		if root.Name() == app {
			return root, nil
		}
	}
	return nil, fmt.Errorf("unknown fixture %q", app)
}

// buildFixtures builds every fixture's root agent.
func buildFixtures(m model.LLM) ([]agent.Agent, error) {
	roots := make([]agent.Agent, 0, len(fixtureBuilders))
	for i, build := range fixtureBuilders {
		a, err := build(m)
		if err != nil {
			return nil, fmt.Errorf("build fixture #%d: %w", i+1, err)
		}
		roots = append(roots, a)
	}
	return roots, nil
}
