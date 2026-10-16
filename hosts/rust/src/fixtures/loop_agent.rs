//! Fixture 6: `loop_agent`, a LoopAgent (max_iterations 3) around `looper`,
//! which has the SDK built-in `exit_loop` tool.

use std::sync::Arc;

use adk_agent::LoopAgent;
use adk_core::{Agent, Llm};

use super::{BuildResult, built, llm};
use crate::tools;

pub fn build(model: Arc<dyn Llm>) -> BuildResult {
    let looper = built(
        "looper",
        llm(
            "looper",
            "Does one unit of work.",
            "AGENT=looper. Do one unit of work; call exit_loop when finished.",
            &model,
        )
        .tool(tools::exit_loop_tool()),
    )?;
    let agent = LoopAgent::new("loop_agent", vec![looper])
        .with_description("Repeats a worker.")
        .with_max_iterations(3);
    Ok(Arc::new(agent) as Arc<dyn Agent>)
}
