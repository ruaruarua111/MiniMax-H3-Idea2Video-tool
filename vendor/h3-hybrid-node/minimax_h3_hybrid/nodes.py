"""MiniMax H3 conditioning with optional keyframes and identity references."""

from __future__ import annotations

import math

import nodes
import node_helpers
from comfy_api.latest import ComfyExtension, io
from comfy_extras.nodes_minimax_h3 import (
    CANVAS_MULTIPLE,
    REF_IMAGE_SHORT_EDGE,
    _empty_av_latent,
    _resize,
)


class MiniMaxH3HybridRefAndKeyframe(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="MiniMaxH3HybridRefAndKeyframe",
            display_name="MiniMax H3 Hybrid Cond (Prompt Studio)",
            category="model/conditioning/minimax",
            description=(
                "Combines optional first/last keyframes with up to nine H3 image "
                "reference blocks in one conditioning payload."
            ),
            inputs=[
                io.Clip.Input("clip"),
                io.Vae.Input("vae"),
                io.Vae.Input("audio_vae"),
                io.String.Input("prompt", multiline=True, dynamic_prompts=True),
                io.Int.Input("width", default=768, min=32, max=nodes.MAX_RESOLUTION, step=32),
                io.Int.Input("height", default=1344, min=32, max=nodes.MAX_RESOLUTION, step=32),
                io.Int.Input("length", default=175, min=5, max=3600, step=17),
                io.Combo.Input("ref_image_size", options=["match", "max"], default="match"),
                io.Image.Input("first_frame", optional=True),
                io.Image.Input("last_frame", optional=True),
                io.Autogrow.Input(
                    "ref_images",
                    optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Image.Input("ref_image"),
                        prefix="ref_image_",
                        min=0,
                        max=9,
                    ),
                ),
                io.Boolean.Input("also_ref_first_frame", default=False),
            ],
            outputs=[io.Conditioning.Output(display_name="positive"), io.Latent.Output()],
        )

    @classmethod
    def execute(
        cls,
        clip,
        vae,
        audio_vae,
        prompt,
        width,
        height,
        length,
        ref_image_size="match",
        first_frame=None,
        last_frame=None,
        ref_images=None,
        also_ref_first_frame=False,
    ):
        del audio_vae  # Required for graph compatibility; native AV latent owns audio.
        latent, frame_count = _empty_av_latent(width, height, length)
        keyframes = []
        keyframe_images = []
        if first_frame is not None:
            image = _resize(first_frame[:1], width, height, "disabled")
            keyframe_images.append(image)
            keyframes.append({"resolved_frame_index": 0, "image": image})
        if last_frame is not None:
            image = _resize(last_frame[:1], width, height, "center")
            keyframe_images.append(image)
            keyframes.append({"resolved_frame_index": frame_count - 1, "image": image})

        ref_items = []
        ref_blocks = []
        for image in (ref_images or {}).values():
            if image is None:
                continue
            source_h, source_w = int(image.shape[1]), int(image.shape[2])
            if ref_image_size == "match":
                scale = min(1.0, math.sqrt((width * height) / (source_w * source_h)))
            else:
                scale = min(1.0, REF_IMAGE_SHORT_EDGE / min(source_w, source_h))
            target_w = max(
                CANVAS_MULTIPLE,
                round(source_w * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE,
            )
            target_h = max(
                CANVAS_MULTIPLE,
                round(source_h * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE,
            )
            resized = _resize(image[:1], target_w, target_h, "disabled")
            ref_items.append({"type": "image", "data": resized})
            ref_blocks.append(
                {
                    "kind": "image",
                    "latent_h": target_h // 16,
                    "latent_w": target_w // 16,
                    "latent": vae.encode(resized),
                }
            )

        if also_ref_first_frame and keyframe_images:
            image = keyframe_images[0]
            ref_items.append({"type": "image", "data": image})
            ref_blocks.append(
                {
                    "kind": "image",
                    "latent_h": height // 16,
                    "latent_w": width // 16,
                    "latent": vae.encode(image),
                }
            )

        if ref_items:
            tokens = clip.tokenize(prompt, minimax_ref_items=ref_items)
        else:
            tokens = clip.tokenize(prompt, images=keyframe_images)
        conditioning = clip.encode_from_tokens_scheduled(tokens)
        values = {}
        if keyframes:
            for keyframe in keyframes:
                keyframe["latent"] = vae.encode(keyframe.pop("image"))
            values["minimax_keyframes"] = keyframes
            values["minimax_frame_count"] = frame_count
        if ref_blocks:
            values["minimax_refs"] = ref_blocks
        if values:
            conditioning = node_helpers.conditioning_set_values(conditioning, values)
        return io.NodeOutput(conditioning, latent)


class MiniMaxH3HybridExtension(ComfyExtension):
    async def get_node_list(self):
        return [MiniMaxH3HybridRefAndKeyframe]
