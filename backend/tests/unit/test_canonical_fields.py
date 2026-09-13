"""Unit tests for canonical field names over Descope payload shapes."""

from app.services.canonical import canonical_tenant, canonical_user

# 2026-09-13T22:11:32+00:00 and 2026-09-14T00:00:00+00:00
CREATED_EPOCH = 1789337492
MODIFIED_EPOCH = 1789344000


def test_canonical_user_adds_id_and_timestamps_beside_the_descope_names():
    descope_user = {
        "userId": "U3JI90VSQhOfc1w5N1VrAqH0SPq7",
        "loginIds": ["e2e@test.example.com"],
        "createdTime": CREATED_EPOCH,
        "modifiedTime": MODIFIED_EPOCH,
        "status": "invited",
    }

    result = canonical_user(descope_user)

    assert result == {
        "userId": "U3JI90VSQhOfc1w5N1VrAqH0SPq7",
        "loginIds": ["e2e@test.example.com"],
        "createdTime": CREATED_EPOCH,
        "modifiedTime": MODIFIED_EPOCH,
        "status": "invited",
        "id": "U3JI90VSQhOfc1w5N1VrAqH0SPq7",
        "created_at": "2026-09-13T22:11:32+00:00",
        "updated_at": "2026-09-14T00:00:00+00:00",
    }
    assert descope_user == {
        "userId": "U3JI90VSQhOfc1w5N1VrAqH0SPq7",
        "loginIds": ["e2e@test.example.com"],
        "createdTime": CREATED_EPOCH,
        "modifiedTime": MODIFIED_EPOCH,
        "status": "invited",
    }, "the caller's payload must not be mutated"


def test_canonical_user_without_a_modified_time_reports_created_at_as_updated_at():
    result = canonical_user({"userId": "U1", "createdTime": CREATED_EPOCH})

    assert result["created_at"] == "2026-09-13T22:11:32+00:00"
    assert result["updated_at"] == "2026-09-13T22:11:32+00:00"


def test_canonical_user_omits_timestamps_it_has_no_source_for():
    assert canonical_user({"userId": "U1"}) == {"userId": "U1", "id": "U1"}


def test_canonical_tenant_maps_timestamps_and_leaves_the_descope_id_alone():
    descope_tenant = {
        "id": "T3BW3wR8YnAGXZ2d1adK69nFd18Y",
        "name": "Acme Corp",
        "createdTime": CREATED_EPOCH,
    }

    result = canonical_tenant(descope_tenant)

    assert result == {
        "id": "T3BW3wR8YnAGXZ2d1adK69nFd18Y",
        "name": "Acme Corp",
        "createdTime": CREATED_EPOCH,
        "created_at": "2026-09-13T22:11:32+00:00",
        "updated_at": "2026-09-13T22:11:32+00:00",
    }


def test_canonical_helpers_pass_non_dict_payloads_through():
    assert canonical_user(None) is None
    assert canonical_tenant("not-a-dict") == "not-a-dict"


def test_canonical_user_ignores_a_bool_or_zero_timestamp():
    # bool is a subclass of int; neither it nor a zero epoch is a real timestamp.
    assert "created_at" not in canonical_user({"userId": "U1", "createdTime": True})
    assert "created_at" not in canonical_user({"userId": "U1", "createdTime": 0})
