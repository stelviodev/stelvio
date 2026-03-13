from stelvio.state_ops import repair_state


def test_repair_state_clears_pending_operations() -> None:
    pending_urn_1 = "urn:pulumi:dev::myapp::aws:iam/role:Role::myapp-dev-old-role"
    pending_urn_2 = "urn:pulumi:dev::myapp::aws:lambda/function:Function::myapp-dev-old-fn"
    state = {
        "checkpoint": {
            "latest": {
                "resources": [
                    {
                        "urn": "urn:pulumi:dev::myapp::aws:s3/bucketV2:BucketV2::uploads",
                        "type": "aws:s3/bucketV2:BucketV2",
                    }
                ],
                "pending_operations": [
                    {"type": "creating", "resource": {"urn": pending_urn_1}},
                    {"type": "deleting", "resource": pending_urn_2},
                ],
            }
        }
    }

    mutations = repair_state(state)

    assert state["checkpoint"]["latest"]["pending_operations"] == []
    pending_mutations = [m for m in mutations if m.action == "remove_pending_operation"]
    assert len(pending_mutations) == 2
    assert pending_mutations[0].target_urn == pending_urn_1
    assert pending_mutations[1].target_urn == pending_urn_2
    assert "creating" in pending_mutations[0].detail
    assert "old-role" in pending_mutations[0].detail
    assert "deleting" in pending_mutations[1].detail
    assert "old-fn" in pending_mutations[1].detail


def test_repair_state_pending_operation_without_resource_uses_unknown_target() -> None:
    state = {
        "checkpoint": {
            "latest": {
                "resources": [],
                "pending_operations": [{"type": "updating", "resource": {}}],
            }
        }
    }

    mutations = repair_state(state)

    assert state["checkpoint"]["latest"]["pending_operations"] == []
    assert len(mutations) == 1
    assert mutations[0].action == "remove_pending_operation"
    assert mutations[0].target_urn == ""
    assert "<unknown-resource>" in mutations[0].detail
