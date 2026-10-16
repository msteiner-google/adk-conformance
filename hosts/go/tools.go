package main

// Shared tool library (fixtures/FIXTURES.md, "Shared tool library").
// Tool parameters carry no descriptions; only the function has one.

import (
	"errors"
	"fmt"

	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/tool"
	"google.golang.org/adk/v2/tool/exitlooptool"
	"google.golang.org/adk/v2/tool/functiontool"
)

type addArgs struct {
	A int `json:"a"`
	B int `json:"b"`
}

// newAddTool: add(a: int, b: int) -> int. Returns a bare integer; the SDK
// wraps non-object results as {"result": ...}.
func newAddTool() (tool.Tool, error) {
	return functiontool.New(functiontool.Config{
		Name:        "add",
		Description: "Adds two integers.",
	}, func(_ agent.Context, args addArgs) (int, error) {
		return args.A + args.B, nil
	})
}

type getWeatherArgs struct {
	City string `json:"city"`
}

// newGetWeatherTool: get_weather(city: str) -> dict.
func newGetWeatherTool() (tool.Tool, error) {
	return functiontool.New(functiontool.Config{
		Name:        "get_weather",
		Description: "Returns the weather for a city.",
	}, func(_ agent.Context, args getWeatherArgs) (map[string]any, error) {
		return map[string]any{
			"city":          args.City,
			"forecast":      "sunny",
			"temperature_c": 21,
		}, nil
	})
}

type setStateArgs struct {
	Key   string `json:"key"`
	Value string `json:"value"`
}

// newSetStateTool: set_state(key: str, value: str) -> dict. Writes
// state[key] = value through the tool context.
func newSetStateTool() (tool.Tool, error) {
	return functiontool.New(functiontool.Config{
		Name:        "set_state",
		Description: "Stores a value in session state.",
	}, func(ctx agent.Context, args setStateArgs) (map[string]any, error) {
		if err := ctx.State().Set(args.Key, args.Value); err != nil {
			return nil, fmt.Errorf("set state %q: %w", args.Key, err)
		}
		return map[string]any{"status": "ok"}, nil
	})
}

// newFailAlwaysTool: fail_always() -> dict. Returns Go's normal tool error.
func newFailAlwaysTool() (tool.Tool, error) {
	return functiontool.New(functiontool.Config{
		Name:        "fail_always",
		Description: "Always fails.",
	}, func(_ agent.Context, _ struct{}) (map[string]any, error) {
		return nil, errors.New("tool failed on purpose")
	})
}

type requestApprovalArgs struct {
	Amount int `json:"amount"`
}

// newRequestApprovalTool: request_approval(amount: int) -> dict, long-running.
func newRequestApprovalTool() (tool.Tool, error) {
	return functiontool.New(functiontool.Config{
		Name:          "request_approval",
		Description:   "Requests human approval for an expense.",
		IsLongRunning: true,
	}, func(_ agent.Context, _ requestApprovalArgs) (map[string]any, error) {
		return map[string]any{"status": "pending", "ticket": "T-1"}, nil
	})
}

// newExitLoopTool: exit_loop() -> dict, via the SDK built-in
// (tool/exitlooptool), which sets Escalate on the tool context actions.
func newExitLoopTool() (tool.Tool, error) {
	return exitlooptool.New()
}

// newTools builds tools in order, stopping at the first error.
func newTools(ctors ...func() (tool.Tool, error)) ([]tool.Tool, error) {
	tools := make([]tool.Tool, 0, len(ctors))
	for _, ctor := range ctors {
		t, err := ctor()
		if err != nil {
			return nil, err
		}
		tools = append(tools, t)
	}
	return tools, nil
}
