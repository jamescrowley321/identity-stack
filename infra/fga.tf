# FGA Authorization Schema
# Defines the document access control model using Descope's AuthZ DSL.
# Relations: owner, editor, viewer (with inheritance).
# Permissions: can_view, can_edit, can_delete derived from relations.

resource "descope_fga_schema" "main" {
  schema = <<-EOT
model AuthZ 1.0

type user

type document
  relation owner: user
  relation editor: user
  relation viewer: user | editor | owner
  permission can_view: viewer
  permission can_edit: editor | owner
  permission can_delete: owner
EOT
}
