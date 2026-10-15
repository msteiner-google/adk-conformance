"""Generates one test per (scenario, subject)."""

load("@rules_python//python:py_test.bzl", "py_test")
load(":subjects.bzl", "SUBJECTS")

def conformance_suite(name, scenarios, subjects = None, size = "medium", tags = []):
    """Creates `<scenario>_<subject>` tests plus a test_suite named `name`.

    Each test starts the scripted model server and the subject's host, runs the
    scenario, and writes `<scenario>.<subject>.transcript.json` to the test's
    undeclared outputs (bazel-testlogs/.../test.outputs/).

    Args:
      name: name of the test_suite grouping all generated tests.
      scenarios: scenario YAML files.
      subjects: subject names from SUBJECTS; defaults to all of them.
      size: test size for every generated test.
      tags: extra tags for every generated test.
    """
    subjects = subjects or SUBJECTS.keys()
    tests = []
    for scenario in scenarios:
        base = scenario.split("/")[-1].removesuffix(".yaml").removesuffix(".yml")
        for subject in subjects:
            info = SUBJECTS[subject]
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
                ],
                data = [scenario, info.host],
                deps = ["//harness"],
                size = size,
                tags = ["conformance", "subject-" + subject] + tags,
            )
            tests.append(test_name)
    native.test_suite(name = name, tests = tests, tags = ["conformance"])
