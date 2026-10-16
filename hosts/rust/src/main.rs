//! adk-monitor Rust host: serves every fixture in fixtures/FIXTURES.md as a
//! separate app (app name == root agent name) through adk-server's own REST API
//! server (`adk_server::create_app`, routes under `/api`), using the SDK's
//! `MultiAgentLoader`.
//!
//! A2A (host contract, M2):
//! - `--a2a-port N --a2a-app NAME`: also serve fixture NAME (a fresh agent tree)
//!   over A2A v1.0 on 127.0.0.1:N, assembled from adk-server's `a2a::v1`
//!   components (see a2a.rs): card at /.well-known/agent-card.json, JSON-RPC at
//!   /, HTTP+JSON at /rest.
//! - `--remote-a2a-url URL`: base URL of the remote A2A agent the remote_agent
//!   and remote_transfer_agent fixtures call (placeholder when absent).

use std::net::{Ipv4Addr, SocketAddr};
use std::process::ExitCode;
use std::sync::Arc;

use adk_artifact::InMemoryArtifactService;
use adk_core::{Llm, MultiAgentLoader};
use adk_model::GeminiModel;
use adk_server::{ServerConfig, create_app, shutdown_signal};
use adk_session::InMemorySessionService;

mod a2a;
mod fixtures;
mod tools;

const MODEL: &str = "gemini-2.5-flash";
const API_KEY: &str = "scripted-key";

struct Args {
    http_port: u16,
    model_base_url: String,
    a2a_port: Option<u16>,
    a2a_app: Option<String>,
    remote_a2a_url: Option<String>,
}

fn parse_port(name: &str, v: &str) -> Result<u16, String> {
    v.parse::<u16>()
        .map_err(|e| format!("invalid {name} {v:?}: {e}"))
}

fn parse_args() -> Result<Args, String> {
    let mut http_port = None;
    let mut model_base_url = None;
    let mut a2a_port = None;
    let mut a2a_app = None;
    let mut remote_a2a_url = None;
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        let (flag, inline) = match arg.split_once('=') {
            Some((f, v)) => (f.to_string(), Some(v.to_string())),
            None => (arg.clone(), None),
        };
        let mut value = |name: &str| -> Result<String, String> {
            inline
                .clone()
                .or_else(|| it.next())
                .ok_or_else(|| format!("{name} requires a value"))
        };
        match flag.as_str() {
            "--http-port" => {
                let v = value("--http-port")?;
                http_port = Some(
                    v.parse::<u16>()
                        .map_err(|e| format!("invalid --http-port {v:?}: {e}"))?,
                );
            }
            "--model-base-url" => model_base_url = Some(value("--model-base-url")?),
            "--a2a-port" => a2a_port = Some(parse_port("--a2a-port", &value("--a2a-port")?)?),
            "--a2a-app" => a2a_app = Some(value("--a2a-app")?),
            "--remote-a2a-url" => remote_a2a_url = Some(value("--remote-a2a-url")?),
            other => eprintln!("host: ignoring unknown argument {other:?}"),
        }
    }
    if a2a_port.is_some() != a2a_app.is_some() {
        return Err("--a2a-port and --a2a-app go together".to_string());
    }
    Ok(Args {
        http_port: http_port.ok_or("--http-port is required")?,
        model_base_url: model_base_url.ok_or("--model-base-url is required")?,
        a2a_port,
        a2a_app,
        remote_a2a_url,
    })
}

/// adk-gemini joins `models/{model}:{method}` onto the base URL, so the base must
/// include the API version and end with a slash (default is
/// `https://generativelanguage.googleapis.com/v1beta/`).
fn gemini_base_url(model_base_url: &str) -> String {
    let trimmed = model_base_url.trim_end_matches('/');
    if trimmed.ends_with("/v1beta") || trimmed.ends_with("/v1") {
        format!("{trimmed}/")
    } else {
        format!("{trimmed}/v1beta/")
    }
}

async fn run(args: Args) -> Result<(), String> {
    let base_url = gemini_base_url(&args.model_base_url);
    tracing::info!(%base_url, model = MODEL, "configuring Gemini model (AI Studio API)");
    let model = GeminiModel::new_with_base_url(API_KEY, MODEL, &base_url)
        .map_err(|e| format!("failed to create Gemini model: {e}"))?;

    let model: Arc<dyn Llm> = Arc::new(model);

    let remote_card = a2a::remote_card(args.remote_a2a_url.as_deref()).await;
    let agents = fixtures::all(model.clone(), &remote_card)?;
    let names: Vec<String> = agents.iter().map(|a| a.name().to_string()).collect();
    let loader =
        MultiAgentLoader::new(agents).map_err(|e| format!("failed to create agent loader: {e}"))?;

    let config = ServerConfig::new(Arc::new(loader), Arc::new(InMemorySessionService::new()))
        .with_artifact_service(Arc::new(InMemoryArtifactService::new()));

    let app = create_app(config);

    let addr = SocketAddr::from((Ipv4Addr::LOCALHOST, args.http_port));
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .map_err(|e| format!("failed to bind {addr}: {e}"))?;
    tracing::info!("ADK REST API listening on http://{addr}/api (apps {names:?})");
    let rest = async {
        axum::serve(listener, app)
            .with_graceful_shutdown(shutdown_signal())
            .await
            .map_err(|e| format!("server error: {e}"))
    };

    let (Some(a2a_port), Some(a2a_app)) = (args.a2a_port, args.a2a_app) else {
        return rest.await;
    };
    // A fresh agent tree for the A2A side, so it shares no state with the REST apps.
    let agent = fixtures::all(model, &remote_card)?
        .into_iter()
        .find(|a| a.name() == a2a_app)
        .ok_or_else(|| format!("--a2a-app: unknown fixture {a2a_app:?} (apps {names:?})"))?;
    let a2a_addr = SocketAddr::from((Ipv4Addr::LOCALHOST, a2a_port));
    let base = format!("http://{a2a_addr}");
    let a2a_listener = tokio::net::TcpListener::bind(a2a_addr)
        .await
        .map_err(|e| format!("failed to bind {a2a_addr}: {e}"))?;
    let a2a_router = a2a::router(&a2a_app, agent, &base);
    tracing::info!(
        "A2A v1 server for {a2a_app} listening on {base}/ (card {base}{})",
        a2a::CARD_PATH
    );
    let a2a = async {
        axum::serve(a2a_listener, a2a_router)
            .with_graceful_shutdown(shutdown_signal())
            .await
            .map_err(|e| format!("A2A server error: {e}"))
    };
    // Either server stopping (signal or error) ends the process.
    tokio::select! {
        r = rest => r,
        r = a2a => r,
    }
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
                "usage: host --http-port N --model-base-url URL [--a2a-port N --a2a-app NAME] [--remote-a2a-url URL]"
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
            tracing::error!("{e}");
            eprintln!("host: {e}");
            ExitCode::FAILURE
        }
    }
}
