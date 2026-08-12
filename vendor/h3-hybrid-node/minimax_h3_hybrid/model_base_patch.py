"""Narrow compatibility patch for H3 keyframes plus reference blocks.

ComfyUI's MiniMaxH3 payload currently assigns ``cond_video_latents`` once for
keyframes and then overwrites it for refs.  This wrapper changes the value only
when both collections exist; ordinary T2VA, I2VA, FL2VA, and Ref2VA calls are
returned byte-for-byte from the original method.
"""

from __future__ import annotations


_PATCHED = False


def install_hybrid_payload_patch() -> None:
    global _PATCHED
    if _PATCHED:
        return
    from comfy.model_base import MiniMaxH3

    if getattr(MiniMaxH3, "_h3_prompt_studio_hybrid_patch", False):
        _PATCHED = True
        return
    original = MiniMaxH3.extra_conds

    def extra_conds_with_hybrid(self, **kwargs):
        output = original(self, **kwargs)
        keyframes = kwargs.get("minimax_keyframes")
        refs = kwargs.get("minimax_refs")
        if not keyframes or not refs:
            return output
        holder = output.get("minimax_payload")
        payload = getattr(holder, "cond", None)
        if not isinstance(payload, dict):
            return output
        payload["cond_video_latents"] = [item["latent"] for item in keyframes] + [
            item["latent"] for item in refs if "latent" in item
        ]
        audio_latents = [
            item["audio_latent"]
            for item in refs
            if item.get("audio_latent") is not None
        ]
        if audio_latents:
            payload["cond_audio_latents"] = audio_latents
        return output

    MiniMaxH3.extra_conds = extra_conds_with_hybrid
    MiniMaxH3._h3_prompt_studio_hybrid_patch = True
    MiniMaxH3._h3_prompt_studio_original_extra_conds = original
    _PATCHED = True
