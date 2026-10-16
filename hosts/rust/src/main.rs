//! adk-monitor Rust host: serves every fixture in fixtures/FIXTURES.md as a
//! separate app (app name == root agent name) through adk-server's own REST API
//! server (`adk_server::create_app`, routes under `/api`), using the SDK's
//! `MultiAgentLoader`.

use std::net::{Ipv4Addr, SocketAddr};
use std::process::ExitCode;
use std::sync::Arc;

use adk_artifact::InMemoryArtifactService;
use adk_core::{Llm, MultiAgentLoader};
use adk_model::GeminiModel;
use adk_server::{ServerConfig, create_app, shutdown_signal};
use adk_session::InMemorySessionService;

mod fixtures;
mod tools;

const MODEL: &str = "gemini-2.5-flash";
const API_KEY: &str = "scripted-key";

struct Args {
    http_port: u16,
    model_base_url: String,
}

fn parse_args() -> Result<Args, String> {
    let mut http_port = None;
    let mut model_base_url = None;
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
            other => eprintln!("host: ignoring unknown argument {other:?}"),
        }
    }
    Ok(Args {
        http_port: http_port.ok_or("--http-port is required")?,
        model_base_url: model_base_url.ok_or("--model-base-url is required")?,
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

    let agents = fixtures::all(model)?;
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

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .map_err(|e| format!("server error: {e}"))
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
            eprintln!("usage: host --http-port N --model-base-url URL");
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
