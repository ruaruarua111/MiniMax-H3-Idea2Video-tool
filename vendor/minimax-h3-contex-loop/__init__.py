"""ComfyUI MiniMax H3 Contex Loop.

Disk-backed recursive MiniMax H3 scene loops with frame-exact picture/audio
continuation, review gates, checkpoint resume, and final assembly.

This project continues the looping work that grew from NikoDemon80's original
ComfyUI-H3-Motion-Context. It intentionally uses distinct public node ids and
vendors upstream's shared runtime-patch ABI so both packs can be installed
together without wrapping ComfyUI twice. The
original Motion Context, Save Latent, and Load Latent ids remain exclusively
owned by Niko's upstream pack; this pack exports its stricter Loop Trim and the
specialized H3 Chain nodes.

Registers the loop nodes without changing ComfyUI's runtime behavior. Chain
On current ComfyUI, Context activates both internal patches inline on first
execution:

  patch_layout   lifts the first/last-only keyframe anchor restriction,
                 moves pinned audio onto the clip's own timeline, and
                 keeps anchor coordinates aligned when refs shift the
                 layout cursor
  patch_payload  stops the refs branch clobbering keyframe cond latents,
                 so pinned video and pinned audio can be used together

Both wrappers are marker-gated. Niko's upstream copy and this vendored copy
recognize the same patch-ownership markers; whichever activates second stands
down. H3 workflows that use neither pack remain stock. If either self-test
fails the nodes still load but refuse the affected path, so an upstream
ComfyUI change produces a clear message rather than a silently wrong render.

When ComfyUI's native MiniMax H3 Add Guide API is available, core owns
arbitrary-position video/audio guides and keyframe/ref payload merging. This
pack switches automatically to native guide records and retains only a small,
marker-gated Ref2VA target-origin alignment wrapper.
"""

from .nodes import (
    NODE_CLASS_MAPPINGS as _CONTEXT_NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS as _CONTEXT_NODE_DISPLAY_NAME_MAPPINGS,
)
from .chain_nodes import (
    CHAIN_NODE_CLASS_MAPPINGS,
    CHAIN_NODE_DISPLAY_NAME_MAPPINGS,
)

NODE_CLASS_MAPPINGS = dict(_CONTEXT_NODE_CLASS_MAPPINGS)
NODE_CLASS_MAPPINGS.update(CHAIN_NODE_CLASS_MAPPINGS)

NODE_DISPLAY_NAME_MAPPINGS = dict(_CONTEXT_NODE_DISPLAY_NAME_MAPPINGS)
NODE_DISPLAY_NAME_MAPPINGS.update(CHAIN_NODE_DISPLAY_NAME_MAPPINGS)

WEB_DIRECTORY = "./web"

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]
