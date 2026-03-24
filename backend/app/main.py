import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.middleware.auth import TokenValidationMiddleware
from app.models.database import create_db_and_tables
from app.routers import accesskeys, attributes, auth, health, protected, roles, tenants


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(title="Descope SaaS Starter API", lifespan=lifespan)

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
app.include_router(auth.router, prefix="/api")
app.include_router(protected.router, prefix="/api")
app.include_router(tenants.router, prefix="/api")
app.include_router(roles.router, prefix="/api")
app.include_router(attributes.router, prefix="/api")
app.include_router(accesskeys.router, prefix="/api")
