# Permissions
# Organized by resource domain: projects, members, documents, settings, billing

resource "descope_permission" "projects_create" {
  name        = "projects.create"
  description = "Create new projects/tenants"
}

resource "descope_permission" "projects_read" {
  name        = "projects.read"
  description = "View projects and their settings"
}

resource "descope_permission" "projects_update" {
  name        = "projects.update"
  description = "Update project settings"
}

resource "descope_permission" "projects_delete" {
  name        = "projects.delete"
  description = "Delete projects"
}

resource "descope_permission" "members_invite" {
  name        = "members.invite"
  description = "Invite new members to a tenant"
}

resource "descope_permission" "members_remove" {
  name        = "members.remove"
  description = "Remove members from a tenant"
}

resource "descope_permission" "members_update_role" {
  name        = "members.update_role"
  description = "Change a member's role within a tenant"
}

resource "descope_permission" "documents_read" {
  name        = "documents.read"
  description = "View documents and resources"
}

resource "descope_permission" "documents_write" {
  name        = "documents.write"
  description = "Create and edit documents and resources"
}

resource "descope_permission" "documents_delete" {
  name        = "documents.delete"
  description = "Delete documents and resources"
}

resource "descope_permission" "settings_manage" {
  name        = "settings.manage"
  description = "Manage tenant settings and configuration"
}

resource "descope_permission" "billing_manage" {
  name        = "billing.manage"
  description = "Manage billing and subscription settings"
}

# Roles
# Each role includes a curated set of permissions.

resource "descope_role" "owner" {
  name        = "owner"
  description = "Full access including billing"

  permission_names = [
    descope_permission.projects_create.name,
    descope_permission.projects_read.name,
    descope_permission.projects_update.name,
    descope_permission.projects_delete.name,
    descope_permission.members_invite.name,
    descope_permission.members_remove.name,
    descope_permission.members_update_role.name,
    descope_permission.documents_read.name,
    descope_permission.documents_write.name,
    descope_permission.documents_delete.name,
    descope_permission.settings_manage.name,
    descope_permission.billing_manage.name,
  ]
}

resource "descope_role" "admin" {
  name        = "admin"
  description = "Full access except billing"

  permission_names = [
    descope_permission.projects_create.name,
    descope_permission.projects_read.name,
    descope_permission.projects_update.name,
    descope_permission.projects_delete.name,
    descope_permission.members_invite.name,
    descope_permission.members_remove.name,
    descope_permission.members_update_role.name,
    descope_permission.documents_read.name,
    descope_permission.documents_write.name,
    descope_permission.documents_delete.name,
    descope_permission.settings_manage.name,
  ]
}

resource "descope_role" "member" {
  name        = "member"
  description = "Standard team member with read/write access"

  permission_names = [
    descope_permission.projects_read.name,
    descope_permission.projects_update.name,
    descope_permission.members_invite.name,
    descope_permission.documents_read.name,
    descope_permission.documents_write.name,
  ]
}

resource "descope_role" "viewer" {
  name        = "viewer"
  description = "Read-only access"

  permission_names = [
    descope_permission.projects_read.name,
    descope_permission.documents_read.name,
  ]
}

# Import pre-existing roles into state
import {
  to = descope_role.owner
  id = "owner"
}

import {
  to = descope_role.admin
  id = "admin"
}

import {
  to = descope_role.member
  id = "member"
}

import {
  to = descope_role.viewer
  id = "viewer"
}

# Import pre-existing permissions into state
import {
  to = descope_permission.projects_create
  id = "projects.create"
}

import {
  to = descope_permission.projects_read
  id = "projects.read"
}

import {
  to = descope_permission.projects_update
  id = "projects.update"
}

import {
  to = descope_permission.projects_delete
  id = "projects.delete"
}

import {
  to = descope_permission.members_invite
  id = "members.invite"
}

import {
  to = descope_permission.members_remove
  id = "members.remove"
}

import {
  to = descope_permission.members_update_role
  id = "members.update_role"
}

import {
  to = descope_permission.documents_read
  id = "documents.read"
}

import {
  to = descope_permission.documents_write
  id = "documents.write"
}

import {
  to = descope_permission.documents_delete
  id = "documents.delete"
}

import {
  to = descope_permission.settings_manage
  id = "settings.manage"
}

import {
  to = descope_permission.billing_manage
  id = "billing.manage"
}
