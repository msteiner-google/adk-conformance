# Python host (google-adk 2.12.0)

`host.py` serves every fixture in `fixtures/FIXTURES.md` through
`google.adk.cli.fast_api.get_fast_api_app` (the `adk api_server` app), API base
path `""`. One package per fixture lives in `fixtures/<app_name>/`; the shared
tool library and model helper live in `adk_monitor_shared/`, which the agent
loader does not list as an app.

Run natively:

```sh
python hosts/python/host.py --http-port 8080 --model-base-url http://127.0.0.1:18765
```

## SDK built-ins used

- `exit_loop`: `google.adk.tools.exit_loop` (loop_agent). Its docstring is
  "Exits the loop.\n\nCall this function only when you are instructed to do so.",
  it returns `None` and also sets `skip_summarization`.
- `request_approval`: plain function wrapped in
  `google.adk.tools.LongRunningFunctionTool`, which appends a long-running
  NOTE to the tool description.
- `helper`: `google.adk.tools.AgentTool` with default settings (parameter
  `request`).
- `transfer_to_agent`: injected by the SDK for `transfer_agent`'s sub-agents.
