"""Smart constructors for the Access context's value objects."""

from expression import Error, Ok

from app.domain import access


def test_role_name_accepts_a_name():
    assert access.role_name("admin") == Ok(access.RoleName("admin"))


def test_role_name_accepts_the_longest_name():
    longest = "r" * access.MAX_NAME_LENGTH
    assert access.role_name(longest) == Ok(access.RoleName(longest))


def test_role_name_rejects_empty():
    assert access.role_name("") == Error(access.InvalidName(access.NameSubject.ROLE, access.NameFault.EMPTY))


def test_role_name_rejects_too_long():
    too_long = "r" * (access.MAX_NAME_LENGTH + 1)
    assert access.role_name(too_long) == Error(access.InvalidName(access.NameSubject.ROLE, access.NameFault.TOO_LONG))


def test_permission_name_accepts_a_name():
    assert access.permission_name("documents.read") == Ok(access.PermissionName("documents.read"))


def test_permission_name_rejects_empty():
    assert access.permission_name("") == Error(
        access.InvalidName(access.NameSubject.PERMISSION, access.NameFault.EMPTY)
    )


def test_permission_name_rejects_too_long():
    too_long = "p" * (access.MAX_NAME_LENGTH + 1)
    assert access.permission_name(too_long) == Error(
        access.InvalidName(access.NameSubject.PERMISSION, access.NameFault.TOO_LONG)
    )
