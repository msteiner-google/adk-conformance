//! Fixture 2: `tools_agent`, an LlmAgent with the shared tool library.

use std::sync::Arc;

use adk_core::Llm;

use super::{BuildResult, built, llm};
use crate::tools;

pub fn build(model: Arc<dyn Llm>) -> BuildResult {
    built(
        "tools_agent",
        llm(
            "tools_agent",
            "Agent with the shared tool library.",
            "AGENT=tools_agent. Use the tools to answer.",
            &model,
        )
        .tool(tools::add_tool())
        .tool(tools::get_weather_tool())
        .tool(tools::set_state_tool())
        .tool(tools::fail_always_tool())
        .tool(tools::request_approval_tool()),
    )
}
