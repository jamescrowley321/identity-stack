from fastapi import APIRouter, Request

from app.middleware.rate_limit import limiter

router = APIRouter(tags=["Health"])


@router.get("/health")
@limiter.exempt
async def health(request: Request):
    return {"status": "ok"}
