"""Reconciliation router — internal endpoint to trigger drift detection/resolution.

POST /api/internal/reconciliation/run — triggers a full reconciliation pass.
Protected by the same flow sync secret used for other internal endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.dependencies.identity import get_reconciliation_service
from app.errors.problem_detail import result_to_response
from app.middleware.rate_limit import RATE_LIMIT_AUTH, limiter
from app.routers.internal import verify_flow_sync_secret
from app.services.reconciliation import ReconciliationService

router = APIRouter(tags=["Internal"])


@router.post(
    "/internal/reconciliation/run",
    dependencies=[Depends(verify_flow_sync_secret)],
)
@limiter.limit(RATE_LIMIT_AUTH)
async def run_reconciliation(
    request: Request,
    service: ReconciliationService = Depends(get_reconciliation_service),
):
    """Trigger a full reconciliation between canonical Postgres and Descope."""
    result = await service.run()
    return result_to_response(result, request)
