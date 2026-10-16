//! Shared tool library (fixtures/FIXTURES.md, "Shared tool library").
//!
//! Function tools use adk-rust's `#[tool]` macro (re-exported by adk-tool):
//! the doc comment becomes the tool description and the parameter schema is
//! derived from the args struct with schemars. Args fields deliberately have no
//! doc comments so the schema carries no parameter descriptions.
//! `exit_loop` is the SDK built-in `adk_tool::ExitLoopTool` (see `exit_loop()`).

use std::sync::Arc;

use adk_tool::{AdkError, Tool, ToolContext, tool};
use schemars::JsonSchema;
use serde::Deserialize;
use serde_json::{Value, json};

#[derive(Deserialize, JsonSchema)]
pub struct AddArgs {
    a: i64,
    b: i64,
}

/// Adds two integers.
#[tool]
async fn add(args: AddArgs) -> Result<Value, AdkError> {
    Ok(json!(args.a + args.b))
}

#[derive(Deserialize, JsonSchema)]
pub struct GetWeatherArgs {
    city: String,
}

/// Returns the weather for a city.
#[tool]
async fn get_weather(args: GetWeatherArgs) -> Result<Value, AdkError> {
    Ok(json!({"city": args.city, "forecast": "sunny", "temperature_c": 21}))
}

#[derive(Deserialize, JsonSchema)]
pub struct SetStateArgs {
    key: String,
    value: String,
}

/// Stores a value in session state.
#[tool]
async fn set_state(ctx: Arc<dyn ToolContext>, args: SetStateArgs) -> Result<Value, AdkError> {
    // adk-rust tool contexts expose state read-only; writes go through the
    // event actions' state_delta, which the runner commits to the session.
    let mut actions = ctx.actions();
    actions
        .state_delta
        .insert(args.key, Value::String(args.value));
    ctx.set_actions(actions);
    Ok(json!({"status": "ok"}))
}

/// Always fails.
#[tool]
async fn fail_always() -> Result<Value, AdkError> {
    Err(AdkError::tool("tool failed on purpose"))
}

#[derive(Deserialize, JsonSchema)]
pub struct RequestApprovalArgs {
    #[allow(dead_code)]
    amount: i64,
}

/// Requests human approval for an expense.
#[tool(long_running)]
async fn request_approval(_args: RequestApprovalArgs) -> Result<Value, AdkError> {
    Ok(json!({"status": "pending", "ticket": "T-1"}))
}

pub fn add_tool() -> Arc<dyn Tool> {
    Arc::new(Add)
}

pub fn get_weather_tool() -> Arc<dyn Tool> {
    Arc::new(GetWeather)
}

pub fn set_state_tool() -> Arc<dyn Tool> {
    Arc::new(SetState)
}

pub fn fail_always_tool() -> Arc<dyn Tool> {
    Arc::new(FailAlways)
}

pub fn request_approval_tool() -> Arc<dyn Tool> {
    Arc::new(RequestApproval)
}

/// SDK built-in `exit_loop` (sets `escalate` and `skip_summarization`).
pub fn exit_loop_tool() -> Arc<dyn Tool> {
    Arc::new(adk_tool::ExitLoopTool::new())
}
