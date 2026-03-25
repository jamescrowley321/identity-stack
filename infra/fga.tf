# FGA (Fine-Grained Authorization) schema for document-level access control.
#
# Defines a "document" type with owner/editor/viewer relations and
# permission rules for can_view, can_edit, and can_delete.
#
# Requires the descope_fga_schema resource from the jamescrowley321
# fork of terraform-provider-descope (feat/fga-resources branch).

resource "descope_fga_schema" "documents" {
  schema = <<-EOT
    type user

    type document
      relations
        define owner: [user]
        define editor: [user]
        define viewer: [user] or editor or owner
      permissions
        define can_view: viewer
        define can_edit: editor or owner
        define can_delete: owner
  EOT
}
