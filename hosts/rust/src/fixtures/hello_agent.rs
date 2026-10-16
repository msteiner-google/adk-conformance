//! Fixture 1: `hello_agent`, an LlmAgent with no tools.

use std::sync::Arc;

use adk_core::Llm;

use super::{BuildResult, built, llm};

pub fn build(model: Arc<dyn Llm>) -> BuildResult {
    built(
        "hello_agent",
        llm(
            "hello_agent",
            "Hello-world fixture agent.",
            "You are a friendly assistant. Answer in one short sentence.",
            &model,
        ),
    )
}
