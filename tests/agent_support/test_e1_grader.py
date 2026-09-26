from pathlib import Path

from graders.grade import grade_project
from pytest import fixture, mark

CASES = Path(__file__).resolve().parents[2] / "agent-support" / "evals" / "cases"
E1 = CASES / "e1-linked-dynamodb"


@fixture
def e1_checks() -> Path:
    return E1 / "checks.toml"


@mark.parametrize(
    ("solution", "expect_functional", "expect_semantic"),
    [
        ("good", True, {"table_linked": True, "table_name_from_resources": True}),
        ("bad-unlinked", True, {"table_linked": False, "table_name_from_resources": True}),
        (
            "bad-hardcoded-name",
            True,
            {"table_linked": True, "table_name_from_resources": False},
        ),
    ],
)
def test_e1_known_solutions(solution, expect_functional, expect_semantic, e1_checks, app_context):
    project = E1 / "solutions" / solution
    result = grade_project(project, e1_checks)

    assert result.error is None, result.error
    assert result.functional_pass is expect_functional
    assert result.semantic_checks == expect_semantic


def test_e1_fixture_fails_functional(e1_checks, app_context):
    result = grade_project(E1 / "fixture", e1_checks)

    assert result.error is None, result.error
    assert result.functional_pass is False
    assert result.semantic_checks["table_linked"] is False
    assert result.semantic_checks["table_name_from_resources"] is False
