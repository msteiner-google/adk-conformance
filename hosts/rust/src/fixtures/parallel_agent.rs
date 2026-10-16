//! Fixture 5: `parallel_agent`, a ParallelAgent with two LlmAgent branches.

use std::sync::Arc;

use adk_agent::ParallelAgent;
use adk_core::{Agent, Llm};

use super::{BuildResult, built, llm};

pub fn build(model: Arc<dyn Llm>) -> BuildResult {
    let left = built(
        "left",
        llm(
            "left",
            "Left branch.",
            "AGENT=left. Answer briefly.",
            &model,
        ),
    )?;
    let right = built(
        "right",
        llm(
            "right",
            "Right branch.",
            "AGENT=right. Answer briefly.",
            &model,
        ),
    )?;
    let agent = ParallelAgent::new("parallel_agent", vec![left, right])
        .with_description("Runs two agents in parallel.");
    Ok(Arc::new(agent) as Arc<dyn Agent>)
}
