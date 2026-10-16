//! Fixture 9: `remote_agent`, adk-server's A2A v1 remote agent
//! (`RemoteA2aV1Agent`) as the app's root agent, SDK defaults.
//!
//! `RemoteA2aV1Agent` takes an already resolved v1 agent card (it has no card
//! resolution of its own); main.rs resolves it from `--remote-a2a-url` at
//! startup, or uses a placeholder card when the flag is absent.

use std::sync::Arc;

use a2a_protocol_types::AgentCard;
use adk_core::Agent;
use adk_server::a2a::remote_agent::v1_remote::{RemoteA2aV1Agent, RemoteA2aV1Config};

use super::BuildResult;

/// A fresh `remote_agent` for `card`. `streaming: None` is the SDK default
/// (auto-detect from the card's capabilities).
pub fn remote(card: &AgentCard) -> Arc<dyn Agent> {
    Arc::new(RemoteA2aV1Agent::new(RemoteA2aV1Config {
        name: "remote_agent".to_string(),
        description: "Remote A2A agent.".to_string(),
        agent_card: card.clone(),
        streaming: None,
    }))
}

pub fn build(card: &AgentCard) -> BuildResult {
    Ok(remote(card))
}
