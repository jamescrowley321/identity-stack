# identity-stack Ory dev environment.
# Add your deployed dev origin alongside the localhost dev-server URIs.
spa_redirect_uris = [
  "http://localhost:3000",
  "http://localhost:3000/callback",
]
spa_post_logout_redirect_uris = [
  "http://localhost:3000",
]
