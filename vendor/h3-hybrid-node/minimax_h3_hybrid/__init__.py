"""H3 Prompt Studio hybrid conditioning extension."""

from .model_base_patch import install_hybrid_payload_patch
from .nodes import MiniMaxH3HybridExtension

install_hybrid_payload_patch()

__all__ = ["MiniMaxH3HybridExtension"]


async def comfy_entrypoint():
    return MiniMaxH3HybridExtension()
