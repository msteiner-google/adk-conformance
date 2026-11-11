"""Subjects under test.

ADK subjects are one ADK host per in-scope language. `base_path` is where the
SDK's own ADK REST API is mounted; it differs by SDK (Go and Rust serve under
/api), which is itself a cross-SDK difference the API area reports on.

Control subjects are bare A2A hosts (hosts/BARE_HOSTS.md): one A2A SDK with a
fixed executor and no ADK. They run the A2A server scenarios next to the ADK
hosts and serve as the remote side in the interop matrix. `control_for` names
the ADK subject whose A2A layer they isolate; `reference` marks a host on a
different A2A SDK than the ADK uses (adk-rust implements A2A itself).

`a2a_server = False` marks an ADK subject whose SDK cannot serve an agent over
A2A v1, `a2a_client = False` one that cannot call an A2A v1 agent. The
scenarios that need the missing side still exist, but report UNSUPPORTED
without starting a host (a capability gap, not a failure).

Paused subjects keep their host in the tree, but get no tests: their entries
move to PAUSED_SUBJECTS and their packages go to .bazelignore, so
`bazel test //...` neither builds nor runs them (README.md, "Paused
subjects"). harness/subjects.py mirrors the paused names for the Python tools.
"""

SUBJECTS = {
    "python": struct(host = "//hosts/python:host", base_path = "", kind = "adk"),
    "go": struct(host = "//hosts/go:host", base_path = "/api", kind = "adk"),
    # adk-java 1.14.0 speaks A2A 0.3 only (google/adk-java#1194) and ships no A2A server.
    "java": struct(host = "//hosts/java:host", base_path = "", kind = "adk", a2a_server = False, a2a_client = False),
    # adk-kotlin 1.5.0 has an A2A client (A2AAgent) but no A2A server.
    "kotlin": struct(host = "//hosts/kotlin:host", base_path = "", kind = "adk", a2a_server = False),
}

CONTROL_SUBJECTS = {
    "a2a-python": struct(host = "//hosts/a2a-python:host", base_path = "", kind = "bare", control_for = "python", reference = False),
    "a2a-go": struct(host = "//hosts/a2a-go:host", base_path = "", kind = "bare", control_for = "go", reference = False),
}

# Paused 2026-11-06: adk-rust and its a2a-rs reference host. To resume, move
# the entries back into SUBJECTS / CONTROL_SUBJECTS, drop hosts/rust and
# hosts/a2a-rs from .bazelignore, and empty PAUSED in harness/subjects.py.
PAUSED_SUBJECTS = {
    "rust": struct(host = "//hosts/rust:host", base_path = "/api", kind = "adk"),
    "a2a-rs": struct(host = "//hosts/a2a-rs:host", base_path = "", kind = "bare", control_for = "rust", reference = True),
}

ALL_SUBJECTS = dict(SUBJECTS.items() + CONTROL_SUBJECTS.items())
