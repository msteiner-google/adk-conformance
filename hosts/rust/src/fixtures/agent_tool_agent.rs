//! Fixture 7: `agent_tool_agent`, an LlmAgent whose only tool is the SDK's
//! AgentTool wrapping the `helper` LlmAgent (default AgentTool settings).

use std::sync::Arc;

use adk_core::Llm;
use adk_tool::AgentTool;

use super::{BuildResult, built, llm};

pub fn build(model: Arc<dyn Llm>) -> BuildResult {
    let helper = built(
        "helper",
        llm(
            "helper",
            "Answers questions for the parent.",
            "AGENT=helper. Answer the question.",
            &model,
        ),
    )?;
    built(
        "agent_tool_agent",
        llm(
            "agent_tool_agent",
            "Delegates to a helper agent used as a tool.",
            "AGENT=agent_tool_agent. Ask the helper tool.",
            &model,
        )
        .tool(Arc::new(AgentTool::new(helper))),
    )
}
