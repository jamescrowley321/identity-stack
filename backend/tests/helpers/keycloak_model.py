"""The fixture realm's token model, as a backend that accepts its tokens has to be configured.

``keycloak/fixtures/README.md`` describes the realm: ``member`` is a portable
type whose one portable permission is ``documents:read``, and ``tenant-admin``
is organization-bound. A test names the type clients it accepts and gets the
settings that go with them, as ``keycloak_config`` arguments, as
``build_provider_configs`` arguments or as environment variables.
"""

MEMBER_TYPE_CLIENT = "member"
TENANT_ADMIN_TYPE_CLIENT = "tenant-admin"
MEMBER_APP_CLIENT = "member-app"
TENANT_ADMIN_APP_CLIENT = "tenant-admin-app"
DOCUMENTS_READ = "documents:read"
# The realm's read-only organization lookup client. Local fixture values.
DIRECTORY_CLIENT_ID = "identity-stack-directory"
DIRECTORY_CLIENT_SECRET = "local-only-directory-client"

_APP_CLIENTS = {MEMBER_TYPE_CLIENT: MEMBER_APP_CLIENT, TENANT_ADMIN_TYPE_CLIENT: TENANT_ADMIN_APP_CLIENT}
_PORTABLE_PERMISSIONS = {MEMBER_TYPE_CLIENT: frozenset({DOCUMENTS_READ})}
_ORGANIZATION_BOUND = frozenset({TENANT_ADMIN_TYPE_CLIENT})

# Every variable the token model is read from, for a test that clears the environment.
MODEL_VARIABLES = (
    "KEYCLOAK_APP_CLIENTS",
    "KEYCLOAK_PORTABLE_TYPES",
    "KEYCLOAK_ORGANIZATION_BOUND_TYPES",
    "KEYCLOAK_PORTABLE_PERMISSIONS",
    "KEYCLOAK_DIRECTORY_CLIENT_ID",
    "KEYCLOAK_DIRECTORY_CLIENT_SECRET",
)


def model_settings(*type_clients: str) -> dict:
    """``keycloak_config`` keyword arguments for these type clients of the fixture realm."""
    return {
        "app_clients": {type_client: _APP_CLIENTS[type_client] for type_client in type_clients},
        "portable_types": frozenset(type_clients) - _ORGANIZATION_BOUND,
        "organization_bound_types": frozenset(type_clients) & _ORGANIZATION_BOUND,
        "portable_permissions": {
            type_client: permissions
            for type_client, permissions in _PORTABLE_PERMISSIONS.items()
            if type_client in type_clients
        },
    }


def directory_settings() -> dict:
    """``keycloak_config`` keyword arguments for the realm's organization lookup client."""
    return {"directory_client_id": DIRECTORY_CLIENT_ID, "directory_client_secret": DIRECTORY_CLIENT_SECRET}


def builder_settings(*type_clients: str) -> dict:
    """The same settings under ``build_provider_configs``'s argument names, with the directory's credentials."""
    return {
        **{f"keycloak_{name}": value for name, value in model_settings(*type_clients).items()},
        "keycloak_directory_client_id": DIRECTORY_CLIENT_ID,
        "keycloak_directory_client_secret": DIRECTORY_CLIENT_SECRET,
    }


def model_environment(*type_clients: str) -> dict[str, str]:
    """The same settings as the environment variables a deployment sets."""
    settings = model_settings(*type_clients)
    return {
        "KEYCLOAK_APP_CLIENTS": ",".join(f"{t}={app}" for t, app in sorted(settings["app_clients"].items())),
        "KEYCLOAK_PORTABLE_TYPES": ",".join(sorted(settings["portable_types"])),
        "KEYCLOAK_ORGANIZATION_BOUND_TYPES": ",".join(sorted(settings["organization_bound_types"])),
        "KEYCLOAK_PORTABLE_PERMISSIONS": ",".join(
            f"{t}={permission}"
            for t, permissions in sorted(settings["portable_permissions"].items())
            for permission in sorted(permissions)
        ),
        "KEYCLOAK_DIRECTORY_CLIENT_ID": DIRECTORY_CLIENT_ID,
        "KEYCLOAK_DIRECTORY_CLIENT_SECRET": DIRECTORY_CLIENT_SECRET,
    }


def set_model_environment(monkeypatch, *type_clients: str) -> None:
    for name, value in model_environment(*type_clients).items():
        monkeypatch.setenv(name, value)
