//! Fixture 10: `remote_transfer_agent`, a router LlmAgent whose only
//! sub-agent is a fresh `remote_agent` (fixture 9). Transfer settings are SDK
//! defaults.

use std::sync::Arc;

use a2a_protocol_types::AgentCard;
use adk_core::Llm;

use super::{BuildResult, built, llm, remote_agent};

pub fn build(model: Arc<dyn Llm>, card: &AgentCard) -> BuildResult {
    built(
        "remote_transfer_agent",
        llm(
            "remote_transfer_agent",
            "Routes requests to a remote agent.",
            "AGENT=remote_transfer_agent. Route every request to remote_agent.",
            &model,
        )
        .sub_agent(remote_agent::remote(card)),
    )
}
