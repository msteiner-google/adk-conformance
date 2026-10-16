//! Fixture 4: `sequential_agent`, a SequentialAgent; step_one's output is
//! stored under `topic` and templated into step_two's instruction.

use std::sync::Arc;

use adk_agent::SequentialAgent;
use adk_core::{Agent, Llm};

use super::{BuildResult, built, llm};

pub fn build(model: Arc<dyn Llm>) -> BuildResult {
    let step_one = built(
        "step_one",
        llm(
            "step_one",
            "Picks a topic.",
            "AGENT=step_one. Pick a topic.",
            &model,
        )
        .output_key("topic"),
    )?;
    let step_two = built(
        "step_two",
        llm(
            "step_two",
            "Writes about the topic.",
            "AGENT=step_two. Write one sentence about {topic}.",
            &model,
        ),
    )?;
    let agent = SequentialAgent::new("sequential_agent", vec![step_one, step_two])
        .with_description("Runs two steps in order.");
    Ok(Arc::new(agent) as Arc<dyn Agent>)
}
