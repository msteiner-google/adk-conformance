//! Fixture 8: `template_agent`, whose instruction reads `{user_name}` from
//! session state via the SDK's instruction templating.

use std::sync::Arc;

use adk_core::Llm;

use super::{BuildResult, built, llm};

pub fn build(model: Arc<dyn Llm>) -> BuildResult {
    built(
        "template_agent",
        llm(
            "template_agent",
            "Agent whose instruction reads session state.",
            "AGENT=template_agent. The user's name is {user_name}.",
            &model,
        ),
    )
}
