//! A2A v1.0 side of the host (`--a2a-port N --a2a-app NAME`, `--remote-a2a-url`).
//!
//! adk-server 2.3.0's documented one-liners (`create_app_with_a2a`,
//! `ServerBuilder::with_a2a`, `A2aServer`) mount the legacy 0.3 controller
//! (`/.well-known/agent.json`, `message/send`). The v1.0 implementation ships
//! only as components behind the `a2a-v1` feature (`adk_server::a2a::v1`), with
//! no router or card route of its own, so this module assembles them the way
//! adk-server's README lists the v1 surface: the agent card at
//! `/.well-known/agent-card.json`, the JSON-RPC handler, the REST routes and
//! `A2A-Version` negotiation.

use std::sync::Arc;
use std::time::{Duration, Instant};

use a2a_protocol_types::{AgentCapabilities, AgentCard, AgentInterface, AgentSkill};
use adk_artifact::InMemoryArtifactService;
use adk_core::Agent;
use adk_server::a2a::build_agent_skills;
use adk_server::a2a::client::v1_client::A2aV1Client;
use adk_server::a2a::v1::V1Executor;
use adk_server::a2a::v1::card::{CachedAgentCard, build_v1_agent_card};
use adk_server::a2a::v1::jsonrpc_handler::jsonrpc_handler;
use adk_server::a2a::v1::push::NoOpPushNotificationSender;
use adk_server::a2a::v1::request_handler::RequestHandler;
use adk_server::a2a::v1::rest_handler::rest_router;
use adk_server::a2a::v1::task_store::{InMemoryTaskStore, TaskStore};
use adk_server::a2a::v1::version::version_negotiation;
use adk_session::InMemorySessionService;
use axum::Router;
use axum::extract::State;
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use tokio::sync::RwLock;

pub const CARD_PATH: &str = "/.well-known/agent-card.json";

/// `--remote-a2a-url` default: nothing listens there, so the remote fixtures
/// are listed but not runnable.
pub const PLACEHOLDER_REMOTE_URL: &str = "http://127.0.0.1:9";

const CARD_RESOLVE_TIMEOUT: Duration = Duration::from_secs(10);

/// The v1 card for the remote fixtures. `RemoteA2aV1Agent` needs a resolved
/// card up front, so it is fetched here (adk-server's own v1 client, retrying
/// for up to 10 s while the remote server starts). Without `--remote-a2a-url`,
/// or if the card cannot be fetched, a minimal placeholder card pointing at the
/// URL is used so the remote fixtures still exist.
pub async fn remote_card(remote_a2a_url: Option<&str>) -> AgentCard {
    let Some(url) = remote_a2a_url else {
        return placeholder_card(PLACEHOLDER_REMOTE_URL);
    };
    let deadline = Instant::now() + CARD_RESOLVE_TIMEOUT;
    loop {
        match A2aV1Client::resolve_agent_card(url).await {
            Ok(card) => {
                tracing::info!(%url, name = %card.name, "resolved remote A2A agent card");
                return card;
            }
            Err(e) if Instant::now() >= deadline => {
                tracing::warn!(%url, "could not resolve remote A2A agent card ({e}); using a placeholder card");
                return placeholder_card(url);
            }
            Err(e) => {
                tracing::debug!(%url, "remote A2A agent card not available yet: {e}");
                tokio::time::sleep(Duration::from_millis(200)).await;
            }
        }
    }
}

fn placeholder_card(url: &str) -> AgentCard {
    let url = url.trim_end_matches('/');
    serde_json::from_value(serde_json::json!({
        "name": "remote_agent",
        "description": "Remote A2A agent.",
        "version": "0.0.0",
        "supportedInterfaces": [
            {"url": format!("{url}/"), "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [],
    }))
    .expect("placeholder agent card is valid")
}

/// The v1 agent card for `agent` served at `base` (`http://127.0.0.1:N`):
/// adk-server's `build_v1_agent_card` (JSONRPC interface at `base/`,
/// protocolVersion 1.0, text/plain modes) plus the HTTP+JSON interface at
/// `base/rest`; skills from adk-server's own `build_agent_skills` (there is no
/// v1 skill builder), converted field by field.
fn agent_card(agent: &dyn Agent, base: &str) -> AgentCard {
    let skills = build_agent_skills(agent)
        .into_iter()
        .map(|s| AgentSkill {
            id: s.id,
            name: s.name,
            description: s.description,
            tags: s.tags,
            examples: s.examples,
            input_modes: None,
            output_modes: None,
            security_requirements: None,
        })
        .collect();
    let capabilities = AgentCapabilities::none()
        .with_streaming(true)
        .with_push_notifications(false);
    let mut card = build_v1_agent_card(
        agent.name(),
        agent.description(),
        &format!("{base}/"),
        "1.0.0",
        skills,
        capabilities,
    );
    card.supported_interfaces.push(AgentInterface {
        url: format!("{base}/rest"),
        protocol_binding: "HTTP+JSON".to_string(),
        protocol_version: "1.0".to_string(),
        tenant: None,
    });
    card
}

/// GET /.well-known/agent-card.json from the `CachedAgentCard` (ETag,
/// Last-Modified, `If-None-Match` -> 304), the caching `CachedAgentCard` exists for.
async fn get_card(
    State(card): State<Arc<RwLock<CachedAgentCard>>>,
    headers: HeaderMap,
) -> Response {
    let card = card.read().await;
    let etag = format!("\"{}\"", card.etag);
    let last_modified = card
        .last_modified
        .format("%a, %d %b %Y %H:%M:%S GMT")
        .to_string();
    let mut response = if headers
        .get(header::IF_NONE_MATCH)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| card.matches_etag(v))
    {
        StatusCode::NOT_MODIFIED.into_response()
    } else {
        (
            [(header::CONTENT_TYPE, "application/json")],
            card.card_json.clone(),
        )
            .into_response()
    };
    let h = response.headers_mut();
    if let Ok(v) = HeaderValue::from_str(&etag) {
        h.insert(header::ETAG, v);
    }
    if let Ok(v) = HeaderValue::from_str(&last_modified) {
        h.insert(header::LAST_MODIFIED, v);
    }
    response
}

/// The A2A v1 router for `agent` (a fresh tree of fixture `app_name`), with its
/// own in-memory session/artifact services and task store:
/// - `GET /.well-known/agent-card.json`: the v1 card;
/// - `POST /`: adk-server's v1 JSON-RPC handler;
/// - `/rest/...`: adk-server's v1 REST router;
/// - JSON-RPC and REST behind adk-server's `version_negotiation` middleware.
pub fn router(app_name: &str, agent: Arc<dyn Agent>, base: &str) -> Router {
    let card = Arc::new(RwLock::new(CachedAgentCard::new(agent_card(
        agent.as_ref(),
        base,
    ))));
    let task_store: Arc<dyn TaskStore> = Arc::new(InMemoryTaskStore::new());
    let executor = Arc::new(V1Executor::new(task_store.clone()));
    let runner_config = Arc::new(
        adk_runner::Runner::builder()
            .app_name(app_name)
            .agent(agent)
            .session_service(Arc::new(InMemorySessionService::new()))
            .artifact_service(Arc::new(InMemoryArtifactService::new()))
            .build_config(),
    );
    let handler = Arc::new(RequestHandler::with_runner(
        executor,
        task_store,
        Arc::new(NoOpPushNotificationSender),
        card.clone(),
        runner_config,
    ));

    let rpc = Router::new()
        .route("/", post(jsonrpc_handler))
        .with_state(handler.clone())
        .nest("/rest", rest_router(handler))
        .layer(axum::middleware::from_fn(version_negotiation));
    Router::new()
        .route(CARD_PATH, get(get_card))
        .with_state(card)
        .merge(rpc)
}
