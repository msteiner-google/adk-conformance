"""Subjects under test.

ADK subjects are one ADK host per in-scope language. `base_path` is where the
SDK's own ADK REST API is mounted; it differs by SDK (Go and Rust serve under
/api), which is itself a cross-SDK difference the API area reports on.

Control subjects are bare A2A hosts (hosts/BARE_HOSTS.md): one A2A SDK with a
fixed executor and no ADK. They run the A2A server scenarios next to the ADK
hosts and serve as the remote side in the interop matrix. `control_for` names
the ADK subject whose A2A layer they isolate; `reference` marks a host on a
different A2A SDK than the ADK uses (adk-rust implements A2A itself).
"""

SUBJECTS = {
    "python": struct(host = "//hosts/python:host", base_path = "", kind = "adk"),
    "go": struct(host = "//hosts/go:host", base_path = "/api", kind = "adk"),
    "rust": struct(host = "//hosts/rust:host", base_path = "/api", kind = "adk"),
}

CONTROL_SUBJECTS = {
    "a2a-python": struct(host = "//hosts/a2a-python:host", base_path = "", kind = "bare", control_for = "python", reference = False),
    "a2a-go": struct(host = "//hosts/a2a-go:host", base_path = "", kind = "bare", control_for = "go", reference = False),
    "a2a-rs": struct(host = "//hosts/a2a-rs:host", base_path = "", kind = "bare", control_for = "rust", reference = True),
}

ALL_SUBJECTS = dict(SUBJECTS.items() + CONTROL_SUBJECTS.items())
