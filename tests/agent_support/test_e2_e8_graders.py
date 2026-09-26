from pathlib import Path

from graders.grade import grade_project
from pytest import fixture, mark

CASES = Path(__file__).resolve().parents[2] / "agent-support" / "evals" / "cases"


@fixture
def case_checks(request) -> Path:
    return CASES / request.param / "checks.toml"


@mark.parametrize(
    ("solution", "expect_functional", "expect_semantic"),
    [
        (
            "good",
            True,
            {
                "semantic_table_linked": True,
                "handler_unchanged": True,
                "components_preserved": True,
            },
        ),
        (
            "bad-rewritten-handler",
            True,
            {
                "semantic_table_linked": True,
                "handler_unchanged": False,
                "components_preserved": True,
            },
        ),
        (
            "bad-manual-env",
            False,
            {
                "semantic_table_linked": False,
                "handler_unchanged": True,
                "components_preserved": True,
            },
        ),
    ],
)
def test_e2_known_solutions(solution, expect_functional, expect_semantic, app_context):
    case = CASES / "e2-missing-link"
    result = grade_project(case / "solutions" / solution, case / "checks.toml")
    assert result.error is None, result.error
    assert result.functional_pass is expect_functional
    assert result.semantic_checks == expect_semantic


def test_e2_fixture_fails_functional(app_context):
    case = CASES / "e2-missing-link"
    result = grade_project(case / "fixture", case / "checks.toml")
    assert result.error is None, result.error
    assert result.functional_pass is False
    assert result.semantic_checks["semantic_table_linked"] is False


@mark.parametrize(
    ("solution", "expect_functional", "expect_semantic"),
    [
        (
            "good",
            True,
            {"producer_links_queue": True, "uses_subscribe": True},
        ),
        (
            "bad-inline",
            False,
            {"producer_links_queue": False, "uses_subscribe": False},
        ),
        (
            "bad-no-subscribe",
            False,
            {"producer_links_queue": True, "uses_subscribe": False},
        ),
    ],
)
def test_e3_known_solutions(solution, expect_functional, expect_semantic, app_context):
    case = CASES / "e3-async-queue"
    result = grade_project(case / "solutions" / solution, case / "checks.toml")
    assert result.error is None, result.error
    assert result.functional_pass is expect_functional
    assert result.semantic_checks == expect_semantic


@mark.parametrize(
    ("solution", "expect_functional", "expect_semantic"),
    [
        ("good", True, {"uses_notify_function": True}),
        ("bad-wrong-filter", False, {"uses_notify_function": False}),
        ("bad-notify-queue", False, {"uses_notify_function": False}),
    ],
)
def test_e4_known_solutions(solution, expect_functional, expect_semantic, app_context):
    case = CASES / "e4-s3-notify-function"
    result = grade_project(case / "solutions" / solution, case / "checks.toml")
    assert result.error is None, result.error
    assert result.functional_pass is expect_functional
    assert result.semantic_checks == expect_semantic


@mark.parametrize(
    ("solution", "expect_functional", "expect_semantic"),
    [
        ("good", True, {"uses_notify_queue": True, "uses_subscribe": True}),
        ("bad-direct-function", False, {"uses_notify_queue": False, "uses_subscribe": False}),
    ],
)
def test_e5_known_solutions(solution, expect_functional, expect_semantic, app_context):
    case = CASES / "e5-s3-notify-queue"
    result = grade_project(case / "solutions" / solution, case / "checks.toml")
    assert result.error is None, result.error
    assert result.functional_pass is expect_functional
    assert result.semantic_checks == expect_semantic


@mark.parametrize(
    ("solution", "expect_functional", "expect_semantic"),
    [
        (
            "good",
            True,
            {
                "semantic_one_topic": True,
                "uses_subscribe": True,
                "producer_links_topic": True,
            },
        ),
        (
            "bad-competing-queue",
            False,
            {
                "semantic_one_topic": False,
                "uses_subscribe": False,
                "producer_links_topic": False,
            },
        ),
    ],
)
def test_e6_known_solutions(solution, expect_functional, expect_semantic, app_context):
    case = CASES / "e6-fanout-topic"
    result = grade_project(case / "solutions" / solution, case / "checks.toml")
    assert result.error is None, result.error
    assert result.functional_pass is expect_functional
    assert result.semantic_checks == expect_semantic


@mark.parametrize(
    ("solution", "expect_functional", "expect_semantic"),
    [
        (
            "good",
            True,
            {"same_queue_component": True, "uses_customize": True},
        ),
        (
            "bad-new-queue",
            False,
            {"same_queue_component": False, "uses_customize": False},
        ),
    ],
)
def test_e7_known_solutions(solution, expect_functional, expect_semantic, app_context):
    case = CASES / "e7-queue-customize-kms"
    result = grade_project(case / "solutions" / solution, case / "checks.toml")
    assert result.error is None, result.error
    assert result.functional_pass is expect_functional
    assert result.semantic_checks == expect_semantic


@mark.parametrize(
    ("solution", "expect_functional", "expect_semantic"),
    [
        (
            "good",
            True,
            {
                "names_preserved": True,
                "producer_links_topic": True,
                "table_still_linked": True,
            },
        ),
        (
            "bad-renamed-handler",
            True,
            {
                "names_preserved": False,
                "producer_links_topic": True,
                "table_still_linked": True,
            },
        ),
        (
            "bad-unlinked-topic",
            True,
            {
                "names_preserved": True,
                "producer_links_topic": False,
                "table_still_linked": True,
            },
        ),
    ],
)
def test_e8_known_solutions(solution, expect_functional, expect_semantic, app_context):
    case = CASES / "e8-minimal-modification"
    result = grade_project(case / "solutions" / solution, case / "checks.toml")
    assert result.error is None, result.error
    assert result.functional_pass is expect_functional
    assert result.semantic_checks == expect_semantic


def test_e8_fixture_lacks_topic(app_context):
    case = CASES / "e8-minimal-modification"
    result = grade_project(case / "fixture", case / "checks.toml")
    assert result.error is None, result.error
    assert result.functional_pass is False
