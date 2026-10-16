//! Fixture agents (fixtures/FIXTURES.md), one module per fixture. Each
//! `build` returns the fixture's root agent; app name == root agent name.

use std::sync::Arc;

use a2a_protocol_types::AgentCard;
use adk_agent::LlmAgentBuilder;
use adk_core::{Agent, Llm};

mod agent_tool_agent;
mod hello_agent;
mod loop_agent;
mod parallel_agent;
mod remote_agent;
mod remote_transfer_agent;
mod sequential_agent;
mod template_agent;
mod tools_agent;
mod transfer_agent;

pub type BuildResult = Result<Arc<dyn Agent>, String>;

/// All fixture root agents, in FIXTURES.md order (hello_agent first, so it is
/// the loader's default root). `remote_card` is the v1 agent card the remote
/// fixtures (9, 10) talk to.
pub fn all(model: Arc<dyn Llm>, remote_card: &AgentCard) -> Result<Vec<Arc<dyn Agent>>, String> {
    Ok(vec![
        hello_agent::build(model.clone())?,
        tools_agent::build(model.clone())?,
        transfer_agent::build(model.clone())?,
        sequential_agent::build(model.clone())?,
        parallel_agent::build(model.clone())?,
        loop_agent::build(model.clone())?,
        agent_tool_agent::build(model.clone())?,
        template_agent::build(model.clone())?,
        remote_agent::build(remote_card)?,
        remote_transfer_agent::build(model, remote_card)?,
    ])
}

/// Standard LlmAgent builder with name, description, instruction and model set.
fn llm(name: &str, description: &str, instruction: &str, model: &Arc<dyn Llm>) -> LlmAgentBuilder {
    LlmAgentBuilder::new(name)
        .description(description)
        .instruction(instruction)
        .model(model.clone())
}

fn built(name: &str, builder: LlmAgentBuilder) -> Result<Arc<dyn Agent>, String> {
    builder
        .build()
        .map(|a| Arc::new(a) as Arc<dyn Agent>)
        .map_err(|e| format!("failed to build agent {name}: {e}"))
}
