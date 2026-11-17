# The compose Zitadel (docker compose --profile zitadel, `make zitadel-up`).
# Credentials never go here: export TF_VAR_zitadel_access_token instead.
zitadel_url           = "http://zitadel.localhost:8480"
frontend_url          = "http://localhost:3000"
access_token_lifetime = "15m0s"
