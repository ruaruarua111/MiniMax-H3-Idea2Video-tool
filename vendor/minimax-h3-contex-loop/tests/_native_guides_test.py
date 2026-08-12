"""PR #15439 compatibility: native AV guides with Ref2VA and legacy fallback.

The fake layout mirrors the new frame_count-free API closely enough to prove:
  * payload monkey-patching is skipped;
  * one video guide and one start-anchored audio guide carry the continuation;
  * Ref2VA refs remain in place and core merges both payload families;
  * every guide chained after Motion Context shares the target-origin fix;
  * an unrelated, unmarked native-guide graph remains core-owned.
"""

import importlib.util
import os
import sys
import types

import numpy as np


_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PKG_DIR = os.path.dirname(_TESTS_DIR)
sys.path.insert(0, _TESTS_DIR)
from _mock_harness import make_torch  # noqa: E402


FRAME_RESCALE = 5.0 / 3.0
FRAME_PER_TOKEN = (1, 4, 4, 4, 4)


class Array(np.ndarray):
    def clone(self):
        return self.copy().view(Array)


def _array(value):
    return np.asarray(value, dtype=np.float64).view(Array)


class T:
    def __init__(self, value):
        self.a = np.asarray(value)

    @property
    def shape(self):
        return self.a.shape

    @property
    def ndim(self):
        return self.a.ndim

    def __getitem__(self, index):
        return T(self.a[index])

    def movedim(self, source, destination):
        return T(np.moveaxis(self.a, source, destination))

    def unsqueeze(self, dimension):
        return T(np.expand_dims(self.a, dimension))

    def clone(self):
        return T(self.a.copy())


class Nested:
    def __init__(self, parts):
        self.parts = parts

    def unbind(self):
        return list(self.parts)


def _video_frames(latent_t):
    return sum(FRAME_PER_TOKEN[index % 5] for index in range(latent_t))


def _video_span(latent_t):
    return FRAME_RESCALE * _video_frames(latent_t)


def _native_model_module():
    module = types.ModuleType("comfy.ldm.minimax.model")
    module.FRAME_RESCALE = FRAME_RESCALE
    module.FRAME_PER_TOKEN = FRAME_PER_TOKEN
    module._video_t_spans = lambda latent_t: [
        FRAME_RESCALE * FRAME_PER_TOKEN[index % 5]
        for index in range(latent_t)
    ]

    class PackedLayout:
        def __init__(self, text_len, latent_t, latent_h, latent_w, audio_t,
                     keyframes=None, refs=None):
            segments = [("text", text_len)]
            blocks = [np.column_stack((
                np.arange(text_len, dtype=np.float64),
                np.zeros((text_len, 2), dtype=np.float64),
            ))]
            cursor = float(text_len)

            for keyframe in keyframes or []:
                start = (float(text_len) + FRAME_RESCALE
                         * float(keyframe["resolved_frame_index"]))
                video = keyframe.get("latent")
                if video is not None:
                    count = int(video.shape[2])
                    segments.append(("cond", count))
                    times = [start + sum(module._video_t_spans(index))
                             for index in range(count)]
                    blocks.append(np.column_stack((
                        times, np.zeros((count, 2), dtype=np.float64))))
                audio = keyframe.get("audio_latent")
                if audio is not None:
                    steps = int(audio.shape[-1])
                    segments.append(("cond_audio", steps * 2))
                    times = np.tile(np.arange(steps, dtype=np.float64), 2) + start
                    blocks.append(np.column_stack((
                        times, np.zeros((steps * 2, 2), dtype=np.float64))))

            for ref in refs or []:
                kind = ref["kind"]
                if kind == "image":
                    segments.append(("ref_img", 1))
                    blocks.append(np.array([[cursor, 0.0, 0.0]]))
                    cursor += 1.0
                elif kind == "audio":
                    steps = int(ref.get("ref_audio_t", 0))
                    if steps:
                        segments.append(("ref_audio", steps * 2))
                        times = np.tile(np.arange(steps), 2) + cursor
                        blocks.append(np.column_stack((
                            times, np.zeros((steps * 2, 2)))))
                    cursor += steps
                elif kind in ("video", "video_audio"):
                    steps = int(ref.get("ref_audio_t", 0))
                    if steps:
                        segments.append(("ref_audio", steps * 2))
                        times = np.tile(np.arange(steps), 2) + cursor
                        blocks.append(np.column_stack((
                            times, np.zeros((steps * 2, 2)))))
                    video_t = int(ref["latent_t"])
                    segments.append(("ref_img", video_t))
                    blocks.append(np.column_stack((
                        [cursor + sum(module._video_t_spans(index))
                         for index in range(video_t)],
                        np.zeros((video_t, 2)),
                    )))
                    cursor += max(float(steps), _video_span(video_t))

            segments.append(("audio", audio_t * 2))
            blocks.append(np.column_stack((
                np.tile(np.arange(audio_t), 2) + cursor,
                np.zeros((audio_t * 2, 2)),
            )))
            segments.append(("video", latent_t))
            blocks.append(np.column_stack((
                [cursor + sum(module._video_t_spans(index))
                 for index in range(latent_t)],
                np.zeros((latent_t, 2)),
            )))

            absolute = []
            offset = 0
            for kind, count in segments:
                absolute.append((offset, offset + count, kind))
                offset += count
            self.segments = absolute
            self.position_ids = _array(np.concatenate(blocks))

    module.PackedLayout = PackedLayout
    return module


def main():
    mm = _native_model_module()
    for name in ("comfy", "comfy.ldm", "comfy.ldm.minimax"):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["comfy.ldm.minimax.model"] = mm
    sys.modules["comfy"].ldm = sys.modules["comfy.ldm"]
    sys.modules["comfy.ldm"].minimax = sys.modules["comfy.ldm.minimax"]
    sys.modules["comfy.ldm.minimax"].model = mm
    sys.modules["torch"] = make_torch()

    utils = types.ModuleType("comfy.utils")
    utils.common_upscale = lambda samples, width, height, method, crop: T(
        np.zeros((samples.shape[0], 3, height, width), dtype=np.float32))
    sys.modules["comfy.utils"] = utils
    sys.modules["comfy"].utils = utils

    model_base = types.ModuleType("comfy.model_base")

    class MiniMaxH3:
        def extra_conds(self, **kwargs):
            keyframes = kwargs.get("minimax_keyframes") or []
            refs = kwargs.get("minimax_refs") or []
            payload = {
                "cond_video_latents": [
                    item["latent"] for item in keyframes
                    if item.get("latent") is not None
                ] + [item["latent"] for item in refs if "latent" in item],
                "cond_audio_latents": [
                    item["audio_latent"] for item in keyframes
                    if item.get("audio_latent") is not None
                ] + [item["audio_latent"] for item in refs
                     if item.get("audio_latent") is not None],
            }
            return {"minimax_payload": types.SimpleNamespace(cond=payload)}

    model_base.MiniMaxH3 = MiniMaxH3
    sys.modules["comfy.model_base"] = model_base
    sys.modules["comfy"].model_base = model_base

    captured = {}
    helpers = types.ModuleType("node_helpers")

    def conditioning_set_values(conditioning, values, append=False):
        output = []
        for value, metadata in conditioning:
            metadata = metadata.copy()
            for key, incoming in values.items():
                if append and metadata.get(key) is not None:
                    incoming = metadata[key] + incoming
                metadata[key] = incoming
            output.append([value, metadata])
        captured.clear()
        captured.update(output[0][1])
        return output

    helpers.conditioning_set_values = conditioning_set_values
    sys.modules["node_helpers"] = helpers

    folder_paths = types.ModuleType("folder_paths")
    folder_paths.get_output_directory = lambda: "/tmp"
    sys.modules["folder_paths"] = folder_paths

    safetensors = types.ModuleType("safetensors")
    safetensors_torch = types.ModuleType("safetensors.torch")
    safetensors_torch.load_file = safetensors_torch.save_file = None
    safetensors.torch = safetensors_torch
    sys.modules["safetensors"] = safetensors
    sys.modules["safetensors.torch"] = safetensors_torch

    package = types.ModuleType("h3_native_pkg")
    package.__path__ = [_PKG_DIR]
    sys.modules[package.__name__] = package
    spec = importlib.util.spec_from_file_location(
        package.__name__ + ".nodes", os.path.join(_PKG_DIR, "nodes.py"))
    nodes = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = nodes
    spec.loader.exec_module(nodes)
    layout_patch = sys.modules[package.__name__ + ".patch_layout"]
    payload_patch = sys.modules[package.__name__ + ".patch_payload"]

    latent_t, frame_count, audio_t = 37, 124, 207
    assert _video_frames(latent_t) == frame_count
    height, width = 30, 54
    target = {"samples": Nested([
        T(np.zeros((1, 16, latent_t, height, width))),
        T(np.zeros((1, 32, 2, audio_t))),
    ])}
    previous = {"samples": Nested([
        T(np.zeros((1, 16, latent_t, height, width))),
        T(np.zeros((1, 32, 2, audio_t))),
    ])}
    context = T(np.zeros((124, 480, 864, 3)))

    class VAE:
        def encode(self, images):
            count = int(images.shape[0])
            steps = max(1, (count - 5) // 17 * 5 + 2)
            return T(np.zeros((1, 16, steps, height, width)))

    refs = [
        {"kind": "image", "latent_h": height, "latent_w": width,
         "latent": T(np.zeros((1, 16, 1, height, width)))},
        {"kind": "audio", "ref_audio_t": 3,
         "audio_latent": T(np.zeros((1, 32, 2, 3)))},
    ]
    scene_one = nodes._prepare_native_guide_conditioning(
        [["conditioning", {"minimax_refs": refs}]])
    scene_one_keyframes = scene_one[0][1]["minimax_keyframes"]
    assert len(scene_one_keyframes) == 1
    assert scene_one_keyframes[0].get("latent") is None
    scene_one_keyframes.append({
        "resolved_frame_index": 60,
        "latent": T(np.zeros((1, 16, 1, height, width))),
    })
    scene_one_layout = mm.PackedLayout(
        7, latent_t, height, width, audio_t,
        keyframes=scene_one_keyframes, refs=refs)
    scene_one_origin = layout_patch._target_origin(scene_one_layout)
    scene_one_cond = next(
        start for start, _stop, kind in scene_one_layout.segments
        if kind == "cond")
    assert abs(float(scene_one_layout.position_ids[scene_one_cond, 0])
               - (scene_one_origin + FRAME_RESCALE * 60)) < 1e-9

    output, trim = nodes.MiniMaxH3MotionContext().apply(
        conditioning=[["conditioning", {"minimax_refs": refs}]],
        vae=VAE(), latent=target, context_frames=context, context_length=22,
        encode_mode="video", anchor_mode="head", crop="disabled",
        audio_context_length=22, audio_mode="timeline",
        context_latent=previous,
    )

    assert trim == 22
    assert layout_patch.is_applied() and layout_patch.native_guides_active()
    assert not payload_patch.is_applied()
    priority_status = nodes._claim_inline_patch_ownership()
    assert priority_status == "native guides; layout owned by this pack"
    assert "minimax_frame_count" not in captured
    assert captured["minimax_refs"] == refs
    keyframes = captured["minimax_keyframes"]
    assert len(keyframes) == 2
    assert keyframes[0]["resolved_frame_index"] == 0
    assert tuple(keyframes[0]["latent"].shape)[2] == 7
    assert keyframes[1].get("latent") is None
    assert tuple(keyframes[1]["audio_latent"].shape)[-1] == 37
    assert abs(keyframes[1]["resolved_frame_index"]) < 1e-6

    # Simulate the official Add Guide node chained after Loop Context.
    keyframes.append({
        "resolved_frame_index": 60,
        "latent": T(np.zeros((1, 16, 1, height, width))),
    })
    layout = mm.PackedLayout(
        7, latent_t, height, width, audio_t,
        keyframes=keyframes, refs=refs)
    target_origin = layout_patch._target_origin(layout)
    guide_segments = [(a, kind) for a, _b, kind in layout.segments
                      if kind in ("cond", "cond_audio")]
    assert len(guide_segments) == 3
    for keyframe, (start, _kind) in zip(keyframes, guide_segments):
        expected = (target_origin + FRAME_RESCALE
                    * float(keyframe["resolved_frame_index"]))
        assert abs(float(layout.position_ids[start, 0]) - expected) < 1e-9

    payload = MiniMaxH3().extra_conds(
        minimax_keyframes=keyframes, minimax_refs=refs,
    )["minimax_payload"].cond
    assert len(payload["cond_video_latents"]) == 3
    assert len(payload["cond_audio_latents"]) == 2
    assert output[0][1]["minimax_refs"] == refs

    # The opt-in stock-reference audio mode remains a reference under the
    # native API. It must append to (not replace) the user's Ref2VA blocks,
    # while the visual continuation still uses a native video guide.
    ref_output, ref_trim = nodes.MiniMaxH3MotionContext().apply(
        conditioning=[["conditioning", {"minimax_refs": refs}]],
        vae=VAE(), latent=target, context_frames=context, context_length=22,
        encode_mode="video", anchor_mode="head", crop="disabled",
        audio_context_length=22, audio_mode="ref",
        context_latent=previous,
    )
    ref_metadata = ref_output[0][1]
    assert ref_trim == 22
    assert len(ref_metadata["minimax_keyframes"]) == 1
    assert len(ref_metadata["minimax_refs"]) == len(refs) + 1
    assert ref_metadata["minimax_refs"][:-1] == refs
    assert ref_metadata["minimax_refs"][-1]["kind"] == "audio"
    ref_payload = MiniMaxH3().extra_conds(
        minimax_keyframes=ref_metadata["minimax_keyframes"],
        minimax_refs=ref_metadata["minimax_refs"],
    )["minimax_payload"].cond
    assert len(ref_payload["cond_video_latents"]) == 2
    assert len(ref_payload["cond_audio_latents"]) == 2

    print("native guides: AV continuation + chained Add Guide align after "
          "Ref2VA on scene 1 and continuations; timeline and reference audio "
          "modes retain core payload merging; legacy payload patch skipped")


if __name__ == "__main__":
    main()
