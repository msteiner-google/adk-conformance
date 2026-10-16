// Command go hosts every adk-monitor fixture (fixtures/FIXTURES.md) as a
// separate app behind the ADK Go SDK's own REST API server
// (cmd/launcher/web + the api sublauncher). App name == root agent name.
//
// Usage:
//
//	go run ./hosts/go --http-port 18766 --model-base-url http://127.0.0.1:18765
//
// The REST API is served under the SDK's default path prefix, /api.
//
// SDK built-ins used by the fixtures: exit_loop (tool/exitlooptool),
// AgentTool (tool/agenttool), Sequential/Parallel/LoopAgent
// (agent/workflowagents/...), transfer_to_agent (added by llmagent for
// sub-agents), and agent.NewMultiLoader to serve all apps from one launcher.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strconv"
	"syscall"

	"google.golang.org/genai"

	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/artifact"
	"google.golang.org/adk/v2/cmd/launcher"
	"google.golang.org/adk/v2/cmd/launcher/universal"
	"google.golang.org/adk/v2/cmd/launcher/web"
	"google.golang.org/adk/v2/cmd/launcher/web/api"
	"google.golang.org/adk/v2/memory"
	"google.golang.org/adk/v2/model/gemini"
	"google.golang.org/adk/v2/session"
)

const (
	modelName = "gemini-2.5-flash"
	apiKey    = "scripted-key"
	bindHost  = "127.0.0.1"
)

func main() {
	log.SetOutput(os.Stderr)
	if err := run(); err != nil {
		log.Printf("host failed: %v", err)
		os.Exit(1)
	}
}

func run() error {
	// Parse the contract's flags ourselves; the SDK launcher has its own
	// flag syntax, which we build below.
	fs := flag.NewFlagSet("adk-monitor-go-host", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	httpPort := fs.Int("http-port", 0, "port for the ADK REST API (required), bound to 127.0.0.1")
	modelBaseURL := fs.String("model-base-url", "", "base URL of the scripted Gemini server (required)")
	if err := fs.Parse(os.Args[1:]); err != nil {
		return err
	}
	if *httpPort <= 0 || *httpPort > 65535 {
		return fmt.Errorf("--http-port is required and must be in 1..65535")
	}
	if *modelBaseURL == "" {
		return fmt.Errorf("--model-base-url is required")
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	model, err := gemini.NewModel(ctx, modelName, &genai.ClientConfig{
		APIKey:  apiKey,
		Backend: genai.BackendGeminiAPI,
		HTTPOptions: genai.HTTPOptions{
			BaseURL: *modelBaseURL,
		},
	})
	if err != nil {
		return fmt.Errorf("create gemini model: %w", err)
	}

	roots, err := buildFixtures(model)
	if err != nil {
		return fmt.Errorf("create fixtures: %w", err)
	}
	// One launcher serves all fixtures; the "root" only matters for callers
	// that don't name an app, and is the first fixture (hello_agent).
	loader, err := agent.NewMultiLoader(roots[0], roots[1:]...)
	if err != nil {
		return fmt.Errorf("create agent loader: %w", err)
	}

	config := &launcher.Config{
		AgentLoader:     loader,
		SessionService:  session.InMemoryService(),
		ArtifactService: artifact.InMemoryService(),
		MemoryService:   memory.InMemoryService(),
	}

	// Same composition as cmd/launcher/prod, minus A2A: universal -> web ->
	// api (REST API only, no web UI). Args: "web", the web launcher flags,
	// then the "api" sublauncher keyword, which keeps its default
	// -path_prefix=/api.
	launcherArgs := []string{
		"web",
		"-host", bindHost,
		"-port", strconv.Itoa(*httpPort),
		"api",
	}
	l := universal.NewLauncher(web.NewLauncher(api.NewLauncher()))
	if err := l.Execute(ctx, config, launcherArgs); err != nil {
		return fmt.Errorf("launcher: %w", err)
	}
	return nil
}
