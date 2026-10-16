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
// A2A (host contract, M2): with --a2a-port N --a2a-app NAME the host also
// serves fixture NAME over A2A v1 on 127.0.0.1:N through adk-go's own A2A
// launcher (cmd/launcher/web/a2a): card at /.well-known/agent-card.json,
// JSON-RPC at /a2a/v1/invoke. With --remote-a2a-url URL the remote_agent and
// remote_transfer_agent fixtures call that A2A server (agent/remoteagent/v2).
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
	a2alauncher "google.golang.org/adk/v2/cmd/launcher/web/a2a"
	"google.golang.org/adk/v2/cmd/launcher/web/api"
	"google.golang.org/adk/v2/memory"
	"google.golang.org/adk/v2/model"
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
	a2aPort := fs.Int("a2a-port", 0, "port for the A2A server of --a2a-app, bound to 127.0.0.1")
	a2aApp := fs.String("a2a-app", "", "fixture to serve over A2A on --a2a-port")
	fs.StringVar(&remoteA2AURL, "remote-a2a-url", "http://127.0.0.1:9", "base URL of the remote A2A agent the remote_* fixtures call")
	if err := fs.Parse(os.Args[1:]); err != nil {
		return err
	}
	if (*a2aPort == 0) != (*a2aApp == "") {
		return fmt.Errorf("--a2a-port and --a2a-app go together")
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
	errs := make(chan error, 2)
	go func() { errs <- wrap("launcher", l.Execute(ctx, config, launcherArgs)) }()
	if *a2aApp != "" {
		go func() { errs <- wrap("a2a launcher", serveA2A(ctx, model, *a2aApp, *a2aPort)) }()
	}
	return <-errs
}

func wrap(what string, err error) error {
	if err != nil {
		return fmt.Errorf("%s: %w", what, err)
	}
	return nil
}

// serveA2A serves one fixture (a fresh agent tree) over A2A with adk-go's
// own A2A web sublauncher, on its own port, with its own in-memory services.
func serveA2A(ctx context.Context, m model.LLM, app string, port int) error {
	root, err := buildFixture(m, app)
	if err != nil {
		return err
	}
	config := &launcher.Config{
		AgentLoader:     agent.NewSingleLoader(root),
		SessionService:  session.InMemoryService(),
		ArtifactService: artifact.InMemoryService(),
		MemoryService:   memory.InMemoryService(),
	}
	args := []string{
		"web",
		"-host", bindHost,
		"-port", strconv.Itoa(port),
		"a2a",
		"-a2a_agent_url", fmt.Sprintf("http://%s:%d", bindHost, port),
	}
	l := universal.NewLauncher(web.NewLauncher(a2alauncher.NewLauncher()))
	return l.Execute(ctx, config, args)
}
