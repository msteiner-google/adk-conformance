"""Generates one test per (scenario, subject), or per (scenario, client, server)."""

load("@rules_python//python:py_test.bzl", "py_test")
load(":subjects.bzl", "ALL_SUBJECTS", "SUBJECTS")

def _lint(name, scenarios):
    native.filegroup(
        name = "all_scenarios",
        srcs = scenarios,
        visibility = ["//visibility:public"],
    )
    py_test(
        name = name + "_lint",
        size = "small",
        srcs = ["//harness:lint.py"],
        main = "//harness:lint.py",
        args = ["$(rootpath %s)" % s for s in scenarios],
        data = scenarios,
        deps = ["//harness"],
    )

def _base(scenario):
    return scenario.split("/")[-1].removesuffix(".yaml").removesuffix(".yml")

def conformance_suite(name, scenarios, subjects = None, size = "medium", tags = []):
    """Creates `<scenario>_<subject>` tests plus a test_suite named `name`.

    Each test starts the scripted model server and the subject's host, runs the
    scenario, and writes `<scenario>.<subject>.transcript.json` to the test's
    undeclared outputs (bazel-testlogs/.../test.outputs/).

    Args:
      name: name of the test_suite grouping all generated tests.
      scenarios: scenario YAML files.
      subjects: subject names from ALL_SUBJECTS; defaults to the ADK subjects.
        A2A server scenarios pass ALL_SUBJECTS to include the bare hosts.
      size: test size for every generated test.
      tags: extra tags for every generated test.
    """
    subjects = subjects or SUBJECTS.keys()
    tests = []
    for scenario in scenarios:
        base = _base(scenario)
        for subject in subjects:
            info = ALL_SUBJECTS[subject]
            test_name = "%s_%s" % (base, subject)
            py_test(
                name = test_name,
                srcs = ["//harness:run_scenario.py"],
                main = "//harness:run_scenario.py",
                args = [
                    "--scenario=$(rootpath %s)" % scenario,
                    "--subject=%s" % subject,
                    "--host=$(rootpath %s)" % info.host,
                    "--base-path=%s" % info.base_path,
                    "--kind=%s" % info.kind,
                    "--known-divergences=$(rootpath //:known_divergences.yaml)",
                ],
                data = [scenario, info.host, "//:known_divergences.yaml"],
                deps = ["//harness"],
                size = size,
                tags = ["conformance", "subject-" + subject, "kind-" + info.kind] + tags,
            )
            tests.append(test_name)
    _lint(name, scenarios)
    native.test_suite(name = name, tests = tests, tags = ["conformance"])

def a2a_client_suite(name, scenarios, clients = None, servers = None, size = "medium", tags = []):
    """Creates `<scenario>_<client>_to_<server>` tests for A2A client scenarios.

    The client (an ADK host) runs the scenario's `remote_*` fixtures against
    the server host (ADK or bare), which serves the scenario's `remote.app`
    fixture over A2A behind the harness's recording proxy. Running every client
    against every server gives the interop matrix (attribution experiment 4).

    Args:
      name: name of the test_suite grouping all generated tests.
      scenarios: scenario YAML files (each with `remote: {app: ...}`).
      clients: ADK subject names; defaults to all of them.
      servers: subject names from ALL_SUBJECTS; defaults to the ADK subjects.
      size: test size for every generated test.
      tags: extra tags for every generated test.
    """
    clients = clients or SUBJECTS.keys()
    servers = servers or SUBJECTS.keys()
    tests = []
    for scenario in scenarios:
        base = _base(scenario)
        for client in clients:
            cinfo = SUBJECTS[client]
            for server in servers:
                sinfo = ALL_SUBJECTS[server]
                test_name = "%s_%s_to_%s" % (base, client, server)
                py_test(
                    name = test_name,
                    srcs = ["//harness:run_scenario.py"],
                    main = "//harness:run_scenario.py",
                    args = [
                        "--scenario=$(rootpath %s)" % scenario,
                        "--subject=%s" % client,
                        "--host=$(rootpath %s)" % cinfo.host,
                        "--base-path=%s" % cinfo.base_path,
                        "--server-subject=%s" % server,
                        "--server-host=$(rootpath %s)" % sinfo.host,
                        "--server-kind=%s" % sinfo.kind,
                        "--known-divergences=$(rootpath //:known_divergences.yaml)",
                    ],
                    data = [scenario, cinfo.host, "//:known_divergences.yaml"] +
                           ([] if sinfo.host == cinfo.host else [sinfo.host]),
                    deps = ["//harness"],
                    size = size,
                    tags = ["conformance", "a2a-client", "subject-" + client, "server-" + server] + tags,
                )
                tests.append(test_name)
    _lint(name, scenarios)
    native.test_suite(name = name, tests = tests, tags = ["conformance"])
