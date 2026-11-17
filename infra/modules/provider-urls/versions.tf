terraform {
  # Cross-variable validation (tls_optional reads issuer_url) needs 1.9.
  required_version = ">= 1.9"
}
