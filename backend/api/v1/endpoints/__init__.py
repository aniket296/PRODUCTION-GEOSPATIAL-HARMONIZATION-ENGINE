from backend.api.v1.endpoints.ai import router as ai_router
from backend.api.v1.endpoints.edge import router as edge_router
from backend.api.v1.endpoints.geodetic import router as geodetic_router
from backend.api.v1.endpoints.layers import router as layers_router

__all__ = ["ai_router", "edge_router", "geodetic_router", "layers_router"]

