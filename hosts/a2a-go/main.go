// Command a2a-go is the bare a2a-go host: the a2a-go v2 server with a fixed
// executor and no ADK. It is the control group for the go subject (see
// hosts/BARE_HOSTS.md) and is built at the a2a-go version adk-go resolves
// (the root go.mod).
//
// Serves one fixture over A2A v1 on --a2a-port: the card at
// /.well-known/agent-card.json, JSON-RPC at / and HTTP+JSON under /rest.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"iter"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
)

const (
	bindHost  = "127.0.0.1"
	modelName = "gemini-2.5-flash"
	apiKey    = "scripted-key"
	maxRounds = 5
)

type fixture struct{ description, instruction string }

var fixtures = map[string]fixture{
	"hello_agent": {"Hello-world fixture agent.", "You are a friendly assistant. Answer in one short sentence."},
	"tools_agent": {"Agent with the shared tool library.", "AGENT=tools_agent. Use the tools to answer."},
}

var longRunning = map[string]bool{"request_approval": true}

// runTool is the shared tool library (fixtures/FIXTURES.md), minus the
// long-running tool.
func runTool(name string, args map[string]any) map[string]any {
	num := func(k string) int {
		if f, ok := args[k].(float64); ok {
			return int(f)
		}
		return 0
	}
	switch name {
	case "add":
		return map[string]any{"result": num("a") + num("b")}
	case "get_weather":
		return map[string]any{"city": args["city"], "forecast": "sunny", "temperature_c": 21}
	case "set_state":
		return map[string]any{"status": "ok"}
	case "fail_always":
		return map[string]any{"error": "tool failed on purpose"}
	}
	return map[string]any{"error": "unknown tool " + name}
}

type content = map[string]any

type executor struct {
	instruction  string
	modelBaseURL string
	mu           sync.Mutex
	history      map[string][]content // contextId -> Gemini contents
}

func (e *executor) callModel(ctx context.Context, contents []content) (content, error) {
	body, _ := json.Marshal(map[string]any{
		"systemInstruction": map[string]any{"parts": []any{map[string]any{"text": e.instruction}}},
		"contents":          contents,
	})
	url := strings.TrimRight(e.modelBaseURL, "/") + "/v1beta/models/" + modelName + ":generateContent"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-goog-api-key", apiKey)
	resp, err := (&http.Client{Timeout: 60 * time.Second}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 300 {
		return nil, fmt.Errorf("model error %d: %s", resp.StatusCode, string(data))
	}
	var parsed struct {
		Candidates []struct {
			Content content `json:"content"`
		} `json:"candidates"`
	}
	if err := json.Unmarshal(data, &parsed); err != nil {
		return nil, err
	}
	if len(parsed.Candidates) == 0 || parsed.Candidates[0].Content == nil {
		return content{"role": "model", "parts": []any{}}, nil
	}
	return parsed.Candidates[0].Content, nil
}

func userParts(msg *a2a.Message) []any {
	var parts []any
	for _, p := range msg.Parts {
		if t, ok := p.Content.(a2a.Text); ok {
			parts = append(parts, map[string]any{"text": string(t)})
			continue
		}
		if p.Meta()["adk_type"] == "function_response" {
			parts = append(parts, map[string]any{"functionResponse": p.Data()})
		}
	}
	return parts
}

func agentMessage(info a2a.TaskInfoProvider, parts ...*a2a.Part) *a2a.Message {
	return a2a.NewMessageForTask(a2a.MessageRoleAgent, info, parts...)
}

func (e *executor) Execute(ctx context.Context, ec *a2asrv.ExecutorContext) iter.Seq2[a2a.Event, error] {
	return func(yield func(a2a.Event, error) bool) {
		if ec.StoredTask == nil {
			if !yield(a2a.NewSubmittedTask(ec, ec.Message), nil) {
				return
			}
		}
		if !yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateWorking, nil), nil) {
			return
		}
		e.mu.Lock()
		history := e.history[ec.ContextID]
		if parts := userParts(ec.Message); len(parts) > 0 {
			history = append(history, content{"role": "user", "parts": parts})
		}
		e.mu.Unlock()
		defer func() {
			e.mu.Lock()
			e.history[ec.ContextID] = history
			e.mu.Unlock()
		}()
		for round := 0; round < maxRounds; round++ {
			reply, err := e.callModel(ctx, history)
			if err != nil {
				yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateFailed, agentMessage(ec, a2a.NewTextPart(err.Error()))), nil)
				return
			}
			history = append(history, content{"role": "model", "parts": reply["parts"]})
			parts, _ := reply["parts"].([]any)
			var calls []map[string]any
			var text strings.Builder
			for _, p := range parts {
				pm, _ := p.(map[string]any)
				if fc, ok := pm["functionCall"].(map[string]any); ok {
					calls = append(calls, fc)
				} else if t, ok := pm["text"].(string); ok {
					text.WriteString(t)
				}
			}
			if len(calls) == 0 {
				if !yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateWorking, agentMessage(ec, a2a.NewTextPart(text.String()))), nil) {
					return
				}
				artifact := a2a.NewArtifactEvent(ec, a2a.NewTextPart(text.String()))
				artifact.LastChunk = true
				if !yield(artifact, nil) {
					return
				}
				yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateCompleted, nil), nil)
				return
			}
			for _, call := range calls {
				name, _ := call["name"].(string)
				if longRunning[name] {
					part := a2a.NewDataPart(map[string]any{"id": call["id"], "name": name, "args": call["args"]})
					part.Metadata = map[string]any{"adk_type": "function_call", "adk_is_long_running": true}
					yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateInputRequired, agentMessage(ec, part)), nil)
					return
				}
			}
			var responses []any
			for _, call := range calls {
				name, _ := call["name"].(string)
				args, _ := call["args"].(map[string]any)
				responses = append(responses, map[string]any{"functionResponse": map[string]any{
					"id": call["id"], "name": name, "response": runTool(name, args)}})
			}
			history = append(history, content{"role": "user", "parts": responses})
		}
		yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateFailed, agentMessage(ec, a2a.NewTextPart("too many tool rounds"))), nil)
	}
}

func (e *executor) Cancel(ctx context.Context, ec *a2asrv.ExecutorContext) iter.Seq2[a2a.Event, error] {
	return func(yield func(a2a.Event, error) bool) {
		yield(a2a.NewStatusUpdateEvent(ec, a2a.TaskStateCanceled, nil), nil)
	}
}

func main() {
	log.SetOutput(os.Stderr)
	fs := flag.NewFlagSet("a2a-go-host", flag.ContinueOnError)
	port := fs.Int("a2a-port", 0, "A2A port, bound to 127.0.0.1 (required)")
	app := fs.String("a2a-app", "", "fixture to serve (required)")
	modelBaseURL := fs.String("model-base-url", "", "scripted model server (required)")
	fs.Int("http-port", 0, "ignored (bare hosts have no ADK REST API)")
	if err := fs.Parse(os.Args[1:]); err != nil {
		os.Exit(2)
	}
	fx, ok := fixtures[*app]
	if *port <= 0 || !ok || *modelBaseURL == "" {
		log.Fatalf("usage: host --a2a-port N --a2a-app {hello_agent,tools_agent} --model-base-url URL")
	}
	base := fmt.Sprintf("http://%s:%d", bindHost, *port)
	card := &a2a.AgentCard{
		Name:        *app,
		Description: fx.description,
		Version:     "1.0.0",
		SupportedInterfaces: []*a2a.AgentInterface{
			a2a.NewAgentInterface(base+"/", a2a.TransportProtocolJSONRPC),
			a2a.NewAgentInterface(base+"/rest", a2a.TransportProtocolHTTPJSON),
		},
		Capabilities:       a2a.AgentCapabilities{Streaming: true, PushNotifications: false},
		DefaultInputModes:  []string{"text/plain"},
		DefaultOutputModes: []string{"text/plain"},
		Skills: []a2a.AgentSkill{{
			ID: *app, Name: *app, Description: fx.description, Tags: []string{"adk-monitor"},
		}},
	}
	handler := a2asrv.NewHandler(&executor{
		instruction: fx.instruction, modelBaseURL: *modelBaseURL, history: map[string][]content{},
	})
	mux := http.NewServeMux()
	mux.Handle(a2asrv.WellKnownAgentCardPath, a2asrv.NewStaticAgentCardHandler(card))
	mux.Handle("/rest/", http.StripPrefix("/rest", a2asrv.NewRESTHandler(handler)))
	mux.Handle("/", a2asrv.NewJSONRPCHandler(handler))

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	srv := &http.Server{Addr: fmt.Sprintf("%s:%d", bindHost, *port), Handler: mux}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdown)
	}()
	log.Printf("bare a2a-go host: %s on %s/", *app, base)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("serve: %v", err)
	}
}
