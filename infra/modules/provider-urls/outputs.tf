output "issuer_url" {
  description = "issuer_url, validated."
  value       = var.issuer_url
}

output "frontend_url" {
  description = "frontend_url, validated."
  value       = var.frontend_url
}

output "require_https" {
  description = "The backend's <PROVIDER>_REQUIRE_HTTPS for this issuer: \"false\" only for an http:// (so local) issuer."
  value       = startswith(var.issuer_url, "https://") ? "true" : "false"
}
