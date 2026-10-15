"""Subjects under test: one ADK host per in-scope language.

`base_path` is where the SDK's own ADK REST API is mounted. It differs by SDK
(Go and Rust serve under /api), which is itself a cross-SDK difference the
API area reports on.
"""

SUBJECTS = {
    "python": struct(host = "//hosts/python:host", base_path = ""),
    "go": struct(host = "//hosts/go:host", base_path = "/api"),
    "rust": struct(host = "//hosts/rust:host", base_path = "/api"),
}
