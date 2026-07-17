from fastapi import APIRouter

from app.api import catalog, github_sync, health, imports, matches, model_registry, openfootball, predictions


api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(catalog.router)
api_router.include_router(matches.router)
api_router.include_router(predictions.router)
api_router.include_router(model_registry.router)
api_router.include_router(imports.router)
api_router.include_router(openfootball.router)
api_router.include_router(github_sync.router)
