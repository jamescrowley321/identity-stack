import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.middleware.auth import TokenValidationMiddleware
from app.routers import health, protected

app = FastAPI(title="Descope SaaS Starter API")

# CORS for frontend dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_URL", "http://localhost:3000")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Token validation middleware — skips public paths
app.add_middleware(
    TokenValidationMiddleware,
    descope_project_id=os.getenv("DESCOPE_PROJECT_ID", ""),
    excluded_paths={"/api/health", "/api/validate-id-token", "/docs", "/openapi.json"},
)

app.include_router(health.router, prefix="/api")
app.include_router(protected.router, prefix="/api")
