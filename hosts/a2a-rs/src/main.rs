//! Bare a2a-rs host: the official A2A Rust SDK (a2aproject/a2a-rs: a2a-lf
//! types, a2a-server-lf server) with a fixed executor and no ADK. It is the
//! *reference* for the rust subject (see hosts/BARE_HOSTS.md): adk-rust
//! implements A2A itself on a2a-protocol-types, so there is no same-SDK control.
//!
//! Serves one fixture over A2A v1 on --a2a-port with the SDK's own routers: the
//! card at /.well-known/agent-card.json (`agent_card_router`), JSON-RPC at /
//! (`jsonrpc_router`) and HTTP+JSON under /rest (`rest_router`), all backed by
//! `DefaultRequestHandler` + `InMemoryTaskStore`.

use std::collections::HashMap;
use std::net::{Ipv4Addr, SocketAddr};
use std::process::ExitCode;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use a2a::*;
use a2a_server::{
    AgentExecutor, DefaultRequestHandler, ExecutorContext, InMemoryTaskStore, StaticAgentCard,
};
use futures::stream::BoxStream;
use serde_json::{Map, Value, json};

const MODEL: &str = "gemini-2.5-flash";
const API_KEY: &str = "scripted-key";
const MAX_ROUNDS: usize = 5;

struct Fixture {
    description: &'static str,
    instruction: &'static str,
}

fn fixture(name: &str) -> Option<Fixture> {
    match name {
        "hello_agent" => Some(Fixture {
            description: "Hello-world fixture agent.",
            instruction: "You are a friendly assistant. Answer in one short sentence.",
        }),
        "tools_agent" => Some(Fixture {
            description: "Agent with the shared tool library.",
            instruction: "AGENT=tools_agent. Use the tools to answer.",
        }),
        _ => None,
    }
}

fn is_long_running(name: &str) -> bool {
    name == "request_approval"
}

/// The shared tool library (fixtures/FIXTURES.md), minus the long-running tool.
fn run_tool(name: &str, args: &Map<String, Value>) -> Value {
    let num = |k: &str| args.get(k).and_then(Value::as_f64).map_or(0, |f| f as i64);
    match name {
        "add" => json!({"result": num("a") + num("b")}),
        "get_weather" => json!({
            "city": args.get("city").cloned().unwrap_or(Value::Null),
            "forecast": "sunny",
            "temperature_c": 21,
        }),
        "set_state" => json!({"status": "ok"}),
        "fail_always" => json!({"error": "tool failed on purpose"}),
        other => json!({"error": format!("unknown tool {other}")}),
    }
}

/// Gemini `contents`, kept per contextId.
type History = Vec<Value>;

struct Executor {
    instruction: String,
    model_url: String,
    http: reqwest::Client,
    history: Arc<Mutex<HashMap<String, History>>>,
}

/// One generateContent call; returns the first candidate's content.
async fn call_model(
    http: &reqwest::Client,
    url: &str,
    instruction: &str,
    contents: &[Value],
) -> Result<Value, String> {
    let body = json!({
        "systemInstruction": {"parts": [{"text": instruction}]},
        "contents": contents,
    });
    let resp = http
        .post(url)
        .header("x-goog-api-key", API_KEY)
        .json(&body)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    let status = resp.status();
    let data = resp.text().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        return Err(format!("model error {}: {data}", status.as_u16()));
    }
    let parsed: Value = serde_json::from_str(&data).map_err(|e| e.to_string())?;
    Ok(parsed
        .pointer("/candidates/0/content")
        .filter(|c| c.is_object())
        .cloned()
        .unwrap_or_else(|| json!({"role": "model", "parts": []})))
}

/// A2A user parts -> Gemini parts: text -> {text}; a data part with metadata
/// adk_type=function_response -> {functionResponse: data}.
fn user_parts(msg: &Message) -> Vec<Value> {
    msg.parts
        .iter()
        .filter_map(|p| match &p.content {
            PartContent::Text(t) => Some(json!({"text": t})),
            PartContent::Data(d)
                if p.metadata.as_ref().and_then(|m| m.get("adk_type"))
                    == Some(&json!("function_response")) =>
            {
                Some(json!({"functionResponse": d}))
            }
            _ => None,
        })
        .collect()
}

fn agent_message(task_id: &str, context_id: &str, parts: Vec<Part>) -> Message {
    let mut msg = Message::new(Role::Agent, parts);
    msg.task_id = Some(task_id.to_string());
    msg.context_id = Some(context_id.to_string());
    msg
}

fn status(
    task_id: &str,
    context_id: &str,
    state: TaskState,
    message: Option<Message>,
) -> StreamResponse {
    StreamResponse::StatusUpdate(TaskStatusUpdateEvent {
        task_id: task_id.to_string(),
        context_id: context_id.to_string(),
        status: TaskStatus {
            state,
            message,
            timestamp: None,
        },
        metadata: None,
    })
}

fn name_of(call: &Map<String, Value>) -> String {
    call.get("name")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

impl AgentExecutor for Executor {
    fn execute(
        &self,
        ctx: ExecutorContext,
    ) -> BoxStream<'static, Result<StreamResponse, A2AError>> {
        let instruction = self.instruction.clone();
        let model_url = self.model_url.clone();
        let http = self.http.clone();
        let store = self.history.clone();
        Box::pin(async_stream::stream! {
            let (tid, cid) = ctx.task_info();
            let Some(message) = ctx.message.clone() else {
                yield Err(A2AError::invalid_params("missing message"));
                return;
            };
            // A message without taskId starts a new task (DefaultRequestHandler
            // has already created and stored it as SUBMITTED); emit it first.
            if message.task_id.is_none() {
                yield Ok(StreamResponse::Task(Task {
                    id: tid.clone(),
                    context_id: cid.clone(),
                    status: TaskStatus { state: TaskState::Submitted, message: None, timestamp: None },
                    artifacts: None,
                    history: Some(vec![message.clone()]),
                    metadata: None,
                }));
            }
            yield Ok(status(&tid, &cid, TaskState::Working, None));

            let mut history = store.lock().unwrap().get(&cid).cloned().unwrap_or_default();
            let parts = user_parts(&message);
            if !parts.is_empty() {
                history.push(json!({"role": "user", "parts": parts}));
            }
            let mut events = Vec::new();
            for _ in 0..MAX_ROUNDS {
                let reply = match call_model(&http, &model_url, &instruction, &history).await {
                    Ok(reply) => reply,
                    Err(e) => {
                        events.push(status(&tid, &cid, TaskState::Failed,
                            Some(agent_message(&tid, &cid, vec![Part::text(e)]))));
                        break;
                    }
                };
                let reply_parts = reply.get("parts").cloned().unwrap_or_else(|| json!([]));
                history.push(json!({"role": "model", "parts": reply_parts}));
                let mut calls: Vec<Map<String, Value>> = Vec::new();
                let mut text = String::new();
                for p in reply_parts.as_array().into_iter().flatten() {
                    if let Some(fc) = p.get("functionCall").and_then(Value::as_object) {
                        calls.push(fc.clone());
                    } else if let Some(t) = p.get("text").and_then(Value::as_str) {
                        text.push_str(t);
                    }
                }
                if calls.is_empty() {
                    events.push(status(&tid, &cid, TaskState::Working,
                        Some(agent_message(&tid, &cid, vec![Part::text(text.clone())]))));
                    events.push(StreamResponse::ArtifactUpdate(TaskArtifactUpdateEvent {
                        task_id: tid.clone(),
                        context_id: cid.clone(),
                        artifact: Artifact {
                            artifact_id: new_artifact_id(),
                            name: None,
                            description: None,
                            parts: vec![Part::text(text)],
                            metadata: None,
                            extensions: None,
                        },
                        append: None,
                        last_chunk: Some(true),
                        metadata: None,
                    }));
                    events.push(status(&tid, &cid, TaskState::Completed, None));
                    break;
                }
                if let Some(call) = calls.iter().find(|c| is_long_running(&name_of(c))) {
                    let mut part = Part::data(json!({
                        "id": call.get("id").cloned().unwrap_or(Value::Null),
                        "name": name_of(call),
                        "args": call.get("args").cloned().unwrap_or(Value::Null),
                    }));
                    part.metadata = Some(HashMap::from([
                        ("adk_type".to_string(), json!("function_call")),
                        ("adk_is_long_running".to_string(), json!(true)),
                    ]));
                    events.push(status(&tid, &cid, TaskState::InputRequired,
                        Some(agent_message(&tid, &cid, vec![part]))));
                    break;
                }
                let responses: Vec<Value> = calls
                    .iter()
                    .map(|c| {
                        let args = c.get("args").and_then(Value::as_object).cloned().unwrap_or_default();
                        json!({"functionResponse": {
                            "id": c.get("id").cloned().unwrap_or(Value::Null),
                            "name": name_of(c),
                            "response": run_tool(&name_of(c), &args),
                        }})
                    })
                    .collect();
                history.push(json!({"role": "user", "parts": responses}));
            }
            store.lock().unwrap().insert(cid.clone(), history);
            if events.is_empty() {
                events.push(status(&tid, &cid, TaskState::Failed,
                    Some(agent_message(&tid, &cid, vec![Part::text("too many tool rounds")]))));
            }
            for event in events {
                yield Ok(event);
            }
        })
    }

    fn cancel(&self, ctx: ExecutorContext) -> BoxStream<'static, Result<StreamResponse, A2AError>> {
        let (tid, cid) = ctx.task_info();
        Box::pin(futures::stream::once(async move {
            Ok(status(&tid, &cid, TaskState::Canceled, None))
        }))
    }
}

struct Args {
    a2a_port: u16,
    a2a_app: String,
    model_base_url: String,
}

fn parse_args() -> Result<Args, String> {
    let (mut port, mut app, mut model) = (None, None, None);
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        let (flag, inline) = match arg.split_once('=') {
            Some((f, v)) => (f.to_string(), Some(v.to_string())),
            None => (arg.clone(), None),
        };
        let mut value = || {
            inline
                .clone()
                .or_else(|| it.next())
                .ok_or(format!("{flag} requires a value"))
        };
        match flag.as_str() {
            "--a2a-port" => {
                let v = value()?;
                port = Some(
                    v.parse::<u16>()
                        .map_err(|e| format!("invalid --a2a-port {v:?}: {e}"))?,
                );
            }
            "--a2a-app" => app = Some(value()?),
            "--model-base-url" => model = Some(value()?),
            "--http-port" => {
                value()?; // ignored: bare hosts have no ADK REST API
            }
            other => return Err(format!("unknown argument {other:?}")),
        }
    }
    Ok(Args {
        a2a_port: port.filter(|p| *p > 0).ok_or("--a2a-port is required")?,
        a2a_app: app.ok_or("--a2a-app is required")?,
        model_base_url: model.ok_or("--model-base-url is required")?,
    })
}

async fn shutdown_signal() {
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .expect("install SIGTERM handler");
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {}
        _ = term.recv() => {}
    }
}

async fn run(args: Args) -> Result<(), String> {
    let fx = fixture(&args.a2a_app).ok_or_else(|| format!("unknown fixture {:?}", args.a2a_app))?;
    let addr = SocketAddr::from((Ipv4Addr::LOCALHOST, args.a2a_port));
    let base = format!("http://{addr}");
    let card = AgentCard {
        name: args.a2a_app.clone(),
        description: fx.description.to_string(),
        version: "1.0.0".to_string(),
        // AgentInterface::new sets protocolVersion to a2a::VERSION ("1.0").
        supported_interfaces: vec![
            AgentInterface::new(format!("{base}/"), TRANSPORT_PROTOCOL_JSONRPC),
            AgentInterface::new(format!("{base}/rest"), TRANSPORT_PROTOCOL_HTTP_JSON),
        ],
        capabilities: AgentCapabilities {
            streaming: Some(true),
            push_notifications: Some(false),
            extensions: None,
            extended_agent_card: None,
        },
        default_input_modes: vec!["text/plain".to_string()],
        default_output_modes: vec!["text/plain".to_string()],
        skills: vec![AgentSkill {
            id: args.a2a_app.clone(),
            name: args.a2a_app.clone(),
            description: fx.description.to_string(),
            tags: vec!["adk-monitor".to_string()],
            examples: None,
            input_modes: None,
            output_modes: None,
            security_requirements: None,
        }],
        provider: None,
        documentation_url: None,
        icon_url: None,
        security_schemes: None,
        security_requirements: None,
        signatures: None,
    };
    let executor = Executor {
        instruction: fx.instruction.to_string(),
        model_url: format!(
            "{}/v1beta/models/{MODEL}:generateContent",
            args.model_base_url.trim_end_matches('/')
        ),
        http: reqwest::Client::builder()
            .timeout(Duration::from_secs(60))
            .build()
            .map_err(|e| format!("http client: {e}"))?,
        history: Arc::new(Mutex::new(HashMap::new())),
    };
    // DefaultRequestHandler enforces its own capabilities (e.g. it rejects
    // streaming unless declared), so it gets the card's.
    let handler = Arc::new(
        DefaultRequestHandler::new(executor, InMemoryTaskStore::new())
            .with_capabilities(card.capabilities.clone()),
    );
    let app = axum::Router::new()
        .merge(a2a_server::agent_card::agent_card_router(Arc::new(
            StaticAgentCard::new(card),
        )))
        .merge(a2a_server::jsonrpc::jsonrpc_router(handler.clone()))
        .nest("/rest", a2a_server::rest::rest_router(handler));

    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .map_err(|e| format!("failed to bind {addr}: {e}"))?;
    tracing::info!("bare a2a-rs host: {} on {base}/", args.a2a_app);
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .map_err(|e| format!("serve: {e}"))
}

fn main() -> ExitCode {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();
    let args = match parse_args() {
        Ok(args) => args,
        Err(e) => {
            eprintln!("host: {e}");
            eprintln!(
                "usage: host --a2a-port N --a2a-app {{hello_agent,tools_agent}} --model-base-url URL"
            );
            return ExitCode::from(2);
        }
    };
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("host: failed to start tokio runtime: {e}");
            return ExitCode::FAILURE;
        }
    };
    match runtime.block_on(run(args)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("host: {e}");
            ExitCode::FAILURE
        }
    }
}
