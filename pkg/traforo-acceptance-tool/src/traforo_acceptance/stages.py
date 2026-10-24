"""Declarative acceptance graph. Finalization is mandatory outside this graph."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Stage:
    name: str
    group: str
    needs: tuple[str, ...] = ("prepare",)
    timeout: int = 3600
    estimate: str = "1–15 min"


STAGES = {
    s.name: s
    for s in (
        Stage("prepare", "preparation", (), 900),
        Stage("python", "local"),
        Stage("style", "local"),
        Stage("go-unit", "local"),
        Stage("go-race", "local"),
        Stage("go-vet", "local"),
        Stage("go-lint", "local"),
        Stage("go-fuzz", "local", timeout=900, estimate="3–10 min"),
        Stage("build", "build", timeout=1800, estimate="2–20 min"),
        Stage("distribution", "distribution"),
        Stage("interop", "distribution", ("build", "distribution")),
        Stage("native", "native", ("build", "distribution"), 3600, "5–60 min"),
        Stage("aws-lambda-vpc", "aws", ("build", "distribution"), 7200, "10–40 min"),
        Stage("aws-standalone", "aws", ("build", "distribution"), 14400, "20–90 min"),
        Stage("aws-dev", "aws", ("build", "distribution"), 14400, "20–90 min"),
        Stage("aws-host-key", "aws", ("build", "distribution"), 14400, "20–90 min"),
        Stage("aws-non-vpc", "aws", ("build", "distribution"), 7200, "5–40 min"),
    )
}


def select(only: tuple[str, ...], skip: tuple[str, ...]) -> list[str]:
    def expand(names):
        result = set()
        for name in names:
            matched = {key for key, stage in STAGES.items() if name in (key, stage.group)}
            if not matched:
                raise ValueError(f"Unknown stage/group {name!r}; use 'stages'")
            result.update(matched)
        return result

    excluded = expand(skip)
    selected = (expand(only) if only else set(STAGES)) - excluded
    if not selected:
        raise ValueError("No stages selected")

    def include(name):
        for dependency in STAGES[name].needs:
            if dependency in excluded:
                raise ValueError(f"{name} requires excluded stage {dependency}")
            if dependency not in selected:
                selected.add(dependency)
                include(dependency)

    for name in list(selected):
        include(name)
    return [name for name in STAGES if name in selected]


AWS_TESTS = {
    "aws-standalone": "test_standalone_tls_reconnect_sigint_and_owner_death",
    "aws-dev": "test_dev_function_url_gates_and_recovers_documentdb",
    "aws-host-key": "test_dev_host_key_rotation_is_terminal_until_a_new_session",
    "aws-non-vpc": "test_non_vpc_actual_dev_runs_without_installed_helper",
}

LAMBDA_TESTS = {
    "test_lambda_standalone_https_and_sigint",
    "test_lambda_transport_recovery_and_sigkill",
    "test_lambda_standalone_partial_failure_and_recovery",
    "test_lambda_dev_private_https_and_partial_failure",
    "test_lambda_host_key_rejection_and_peer_isolation",
}
DOCUMENTDB_STAGES = {"aws-standalone", "aws-dev", "aws-host-key"}
