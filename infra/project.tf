# Descope project configuration
# Each phase will add blocks here as features are implemented.

resource "descope_project" "starter" {
  name = var.descope_project_name

  # Phase 1b: Authentication methods will be configured here
  # authentication { ... }

  # Phase 2b: Roles and permissions will be configured here
  # authorization { ... }

  # Phase 2c: Custom attributes will be configured here
  # attributes { ... }

  # Phase 1c: Session/token settings will be configured here
  # project_settings { ... }

  # Phase 4b: JWT templates will be configured here
  # jwt_templates { ... }

  # Phase 5b: Connectors will be configured here
  # connectors { ... }

  # Phase 5d: Applications will be configured here
  # applications { ... }
}
