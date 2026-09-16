# FGA Authorization Schema
# Defines the document access control model using Descope's AuthZ DSL.
# Relations: owner, editor, viewer (with inheritance).
# Permissions: can_view, can_edit, can_delete derived from relations.

# Descope strips the trailing newline from the schema it stores, so a heredoc
# (which always ends in one) never matches what the API reads back. Without
# chomp() every plan reports `descope_fga_schema.main` as changed and every
# apply is a no-op that "succeeds" — a permanent diff that would camouflage a
# real schema wipe, which is exactly the failure this resource has hit before.
resource "descope_fga_schema" "main" {
  schema = chomp(<<-EOT
model AuthZ 1.0

type user

type document
  relation owner: user
  relation editor: user
  relation viewer: user
  permission can_view: viewer | editor | owner
  permission can_edit: editor | owner
  permission can_delete: owner
EOT
  )
}
