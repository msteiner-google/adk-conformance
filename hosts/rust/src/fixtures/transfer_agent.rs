//! Fixture 3: `transfer_agent`, a router LlmAgent with two LlmAgent sub-agents.
//! Transfer settings are SDK defaults.

use std::sync::Arc;

use adk_core::Llm;

use super::{BuildResult, built, llm};

pub fn build(model: Arc<dyn Llm>) -> BuildResult {
    let greeter = built(
        "greeter",
        llm(
            "greeter",
            "Greets the user.",
            "AGENT=greeter. Greet the user.",
            &model,
        ),
    )?;
    let farewell = built(
        "farewell",
        llm(
            "farewell",
            "Says goodbye.",
            "AGENT=farewell. Say goodbye.",
            &model,
        ),
    )?;
    built(
        "transfer_agent",
        llm(
            "transfer_agent",
            "Routes requests to specialists.",
            "AGENT=transfer_agent. Route greetings to greeter and goodbyes to farewell.",
            &model,
        )
        .sub_agent(greeter)
        .sub_agent(farewell),
    )
}
