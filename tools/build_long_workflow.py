#!/usr/bin/env python3
"""Build the frame-exact API workflow used by the long-form scheduler.

No command in this module contacts ComfyUI.  The generated graph is standard
API prompt JSON and all variable values (prompt, seed, image paths and frame
allocation) are injected locally before a job is submitted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from longform import FPS, h3_model_frames, uniform_frame_indices  # noqa: E402


DEFAULT_OUTPUT = PROJECT_ROOT / "comfyui_workflows" / "MiniMax_H3_LongForm_AutoChain_API.json"

VIDEO_MODEL = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
TURBO_LORA = "minimax_h3_turbo_v4_step600_ema.safetensors"
TEXT_ENCODER = "qwen3vl_32b_minimax_h3_bf16.safetensors"
VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"
UPSCALE_MODEL = "RealESRGAN_x2plus.pth"
PROJECT_ROOT_TOKEN = "$IDEA2VIDEO_PROJECT_ROOT"


def _runs(values: Iterable[int]) -> list[tuple[int, int]]:
    items = list(values)
    if not items:
        return []
    result: list[tuple[int, int]] = []
    start = previous = items[0]
    for value in items[1:]:
        if value != previous + 1:
            result.append((start, previous - start + 1))
            start = value
        previous = value
    result.append((start, previous - start + 1))
    return result


def _node(class_type: str, inputs: dict[str, Any], title: str = "") -> dict[str, Any]:
    value: dict[str, Any] = {"class_type": class_type, "inputs": inputs}
    if title:
        value["_meta"] = {"title": title}
    return value


def _project_video_inputs(
    video_link: list[Any], project_root: str, relative_path: str
) -> dict[str, Any]:
    return {
        "video": video_link,
        "project_root": project_root,
        "relative_path": relative_path,
        "crf": 18.0,
    }


def build_api_workflow(
    *,
    prompt: str,
    output_frames: int,
    boundary_before: str,
    seed: int,
    run_id: str,
    segment_index: int,
    attempt: int,
    first_frame: dict[str, str] | None = None,
    last_frame: dict[str, str] | None = None,
    reference_images: list[str | dict[str, str]] | None = None,
    project_root: str = PROJECT_ROOT_TOKEN,
) -> dict[str, Any]:
    continuous = boundary_before == "continuous"
    if continuous and not first_frame:
        raise ValueError("continuous segment requires first_frame")
    if boundary_before not in {"start", "continuous", "cut"}:
        raise ValueError("invalid boundary_before")
    refs = list(reference_images or [])
    if len(refs) > (8 if continuous else 9):
        raise ValueError("too many reference images")
    model_frames = h3_model_frames(output_frames, continuous=continuous)
    selected = uniform_frame_indices(
        model_frames, output_frames, skip_first=continuous
    )
    duration = output_frames / FPS
    project_root = str(project_root or "").strip()
    if not project_root:
        raise ValueError("project_root is required for direct project output")

    graph: dict[str, dict[str, Any]] = {
        "1": _node(
            "UNETLoader",
            {"unet_name": VIDEO_MODEL, "weight_dtype": "default"},
            "MiniMax H3 FL2VA INT8",
        ),
        "2": _node(
            "MiniMaxH3TurboLoRA",
            {
                "model": ["1", 0],
                "lora_name": TURBO_LORA,
                "strength": 1.0,
                "low_vram": False,
            },
            "Turbo v4",
        ),
        "3": _node(
            "CLIPLoader",
            {"clip_name": TEXT_ENCODER, "type": "minimax", "device": "default"},
        ),
        "4": _node("VAELoader", {"vae_name": VIDEO_VAE}),
        "5": _node("VAELoader", {"vae_name": AUDIO_VAE}),
        "6": _node(
            "MiniMaxH3HybridRefAndKeyframe",
            {
                "clip": ["3", 0],
                "vae": ["4", 0],
                "audio_vae": ["5", 0],
                "prompt": prompt,
                "width": 768,
                "height": 1344,
                "length": model_frames,
                "ref_image_size": "match",
                "also_ref_first_frame": bool(first_frame and refs),
            },
            "Prompt Studio Hybrid Conditioning",
        ),
        "7": _node("RandomNoise", {"noise_seed": int(seed)}),
        "8": _node(
            "BasicScheduler",
            {"model": ["2", 0], "scheduler": "simple", "steps": 8, "denoise": 1.0},
        ),
        "9": _node("MiniMaxH3TurboSampler", {}),
        "10": _node("BasicGuider", {"model": ["2", 0], "conditioning": ["6", 0]}),
        "11": _node(
            "SamplerCustomAdvanced",
            {
                "noise": ["7", 0],
                "guider": ["10", 0],
                "sampler": ["9", 0],
                "sigmas": ["8", 0],
                "latent_image": ["6", 1],
            },
        ),
        "12": _node("VAEDecode", {"samples": ["11", 0], "vae": ["4", 0]}),
        "13": _node("VAEDecodeAudio", {"samples": ["11", 0], "vae": ["5", 0]}),
        "14": _node(
            "TrimAudioDuration",
            {
                "audio": ["13", 0],
                "start_index": 1.0 / FPS if continuous else 0.0,
                "duration": duration,
            },
            "Frame-aligned native audio",
        ),
    }

    next_id = 20
    if first_frame:
        loader_type = first_frame.get("type")
        if loader_type not in {"input", "output"}:
            raise ValueError("first_frame type must be input or output")
        loader_class = "LoadImage" if loader_type == "input" else "LoadImageOutput"
        graph[str(next_id)] = _node(
            loader_class,
            {"image": first_frame["name"]},
            "Previous native tail / initial frame",
        )
        graph["6"]["inputs"]["first_frame"] = [str(next_id), 0]
        next_id += 1

    if last_frame:
        loader_type = last_frame.get("type")
        if loader_type not in {"input", "output"}:
            raise ValueError("last_frame type must be input or output")
        image_name = str(last_frame.get("name") or "")
        if not image_name:
            raise ValueError("last_frame name is required")
        loader_class = "LoadImage" if loader_type == "input" else "LoadImageOutput"
        graph[str(next_id)] = _node(
            loader_class,
            {"image": image_name},
            "Exact Picture 2 last frame",
        )
        graph["6"]["inputs"]["last_frame"] = [str(next_id), 0]
        next_id += 1

    for ref_index, ref_value in enumerate(refs):
        if isinstance(ref_value, str):
            ref_type, image_name = "input", ref_value
        elif isinstance(ref_value, dict):
            ref_type = str(ref_value.get("type") or "input")
            image_name = str(ref_value.get("name") or "")
        else:
            raise ValueError("reference image must be a string or object")
        if ref_type not in {"input", "output"} or not image_name:
            raise ValueError("reference image type/name invalid")
        node_id = str(next_id)
        loader_class = "LoadImage" if ref_type == "input" else "LoadImageOutput"
        graph[node_id] = _node(loader_class, {"image": image_name}, f"Identity ref {ref_index + 1}")
        graph["6"]["inputs"][f"ref_images.ref_image_{ref_index}"] = [node_id, 0]
        next_id += 1

    selector_ids: list[str] = []
    for run_index, (start, length) in enumerate(_runs(selected), start=1):
        node_id = str(next_id)
        next_id += 1
        graph[node_id] = _node(
            "ImageFromBatch",
            {"image": ["12", 0], "batch_index": start, "length": length},
            f"Selected model frames run {run_index}",
        )
        selector_ids.append(node_id)
    selected_source = selector_ids[0]
    for selector_id in selector_ids[1:]:
        node_id = str(next_id)
        next_id += 1
        graph[node_id] = _node(
            "ImageBatch",
            {"image1": [selected_source, 0], "image2": [selector_id, 0]},
            "Join selected frames",
        )
        selected_source = node_id

    tail_id = str(next_id)
    next_id += 1
    graph[tail_id] = _node(
        "ImageFromBatch",
        {"image": ["12", 0], "batch_index": model_frames - 1, "length": 1},
        "Native endpoint for next segment",
    )
    save_tail_id = str(next_id)
    next_id += 1
    relative_base = f"{run_id}/segments/{segment_index:04d}/attempt_{attempt}"
    graph[save_tail_id] = _node(
        "H3Idea2VideoProjectImageSave",
        {
            "images": [tail_id, 0],
            "project_root": project_root,
            "relative_path": relative_base + "/tail_native.png",
        },
        "Save lossless native tail directly to project",
    )

    sample_sources: list[str] = []
    for sample_position in sorted(
        {max(0, output_frames // 4), output_frames // 2, min(output_frames - 1, output_frames * 3 // 4)}
    ):
        sample_id = str(next_id)
        next_id += 1
        graph[sample_id] = _node(
            "ImageFromBatch",
            {"image": [selected_source, 0], "batch_index": sample_position, "length": 1},
            f"Identity/QC sample output frame {sample_position}",
        )
        sample_sources.append(sample_id)
    sample_batch = sample_sources[0]
    for sample_id in sample_sources[1:]:
        join_id = str(next_id)
        next_id += 1
        graph[join_id] = _node(
            "ImageBatch",
            {"image1": [sample_batch, 0], "image2": [sample_id, 0]},
            "Join identity/QC samples",
        )
        sample_batch = join_id
    save_samples_id = str(next_id)
    next_id += 1
    graph[save_samples_id] = _node(
        "H3Idea2VideoProjectImageSave",
        {
            "images": [sample_batch, 0],
            "project_root": project_root,
            "relative_path": relative_base + "/qc_sample.png",
        },
        "Save native QC samples directly to project",
    )

    native_video_id = str(next_id)
    next_id += 1
    graph[native_video_id] = _node(
        "CreateVideo",
        {"images": [selected_source, 0], "audio": ["14", 0], "fps": FPS, "bit_depth": 10},
    )
    save_native_id = str(next_id)
    next_id += 1
    graph[save_native_id] = _node(
        "H3Idea2VideoProjectVideoSave",
        _project_video_inputs(
            [native_video_id, 0], project_root, relative_base + "/native_768x1344.mp4"
        ),
        "Save native diagnostic segment directly to project",
    )

    upscale_loader_id = str(next_id)
    next_id += 1
    graph[upscale_loader_id] = _node("UpscaleModelLoader", {"model_name": UPSCALE_MODEL})
    upscale_id = str(next_id)
    next_id += 1
    graph[upscale_id] = _node(
        "ImageUpscaleWithModel",
        {"upscale_model": [upscale_loader_id, 0], "image": [selected_source, 0]},
    )
    resize_id = str(next_id)
    next_id += 1
    graph[resize_id] = _node(
        "ImageScale",
        {
            "image": [upscale_id, 0],
            "upscale_method": "lanczos",
            "width": 1080,
            "height": 1920,
            "crop": "center",
        },
    )
    final_video_id = str(next_id)
    next_id += 1
    graph[final_video_id] = _node(
        "CreateVideo",
        {"images": [resize_id, 0], "audio": ["14", 0], "fps": FPS, "bit_depth": 10},
    )
    save_final_id = str(next_id)
    graph[save_final_id] = _node(
        "H3Idea2VideoProjectVideoSave",
        _project_video_inputs(
            [final_video_id, 0], project_root, relative_base + "/final_1080x1920.mp4"
        ),
        "Save accepted-resolution segment directly to project",
    )

    return {
        "prompt": graph,
        "meta": {
            "schema": "h3-prompt-studio-long-api-v1",
            "run_id": run_id,
            "segment_index": segment_index,
            "attempt": attempt,
            "boundary_before": boundary_before,
            "fps": FPS,
            "output_frames": output_frames,
            "duration_seconds": duration,
            "model_frames": model_frames,
            "selected_indices": selected,
            "tail_source_index": model_frames - 1,
            "project_root": project_root,
            "artifact_directory": "runs/" + relative_base,
            "has_first_frame": bool(first_frame),
            "has_last_frame": bool(last_frame),
            "output_nodes": {
                "tail": save_tail_id,
                "qc_samples": save_samples_id,
                "native_video": save_native_id,
                "final_video": save_final_id,
            },
            "models": {
                "diffusion": VIDEO_MODEL,
                "lora": TURBO_LORA,
                "steps": 8,
                "native_resolution": [768, 1344],
                "final_resolution": [1080, 1920],
                "video_codec": "h264",
                "crf": 18,
            },
        },
    }


def validate_api_workflow(value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    graph = value.get("prompt")
    if not isinstance(graph, dict) or not graph:
        return ["prompt graph missing"]
    for node_id, node in graph.items():
        if not isinstance(node, dict) or not node.get("class_type"):
            errors.append(f"node {node_id} missing class_type")
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            errors.append(f"node {node_id} inputs invalid")
            continue
        for name, item in inputs.items():
            if (
                isinstance(item, list)
                and len(item) == 2
                and isinstance(item[0], str)
                and isinstance(item[1], int)
                and item[0] not in graph
            ):
                errors.append(f"node {node_id} input {name} references {item[0]}")
    meta = value.get("meta") or {}
    output_nodes = meta.get("output_nodes") or {}
    for name in ("tail", "qc_samples", "native_video", "final_video"):
        if str(output_nodes.get(name) or "") not in graph:
            errors.append(f"missing output node {name}")
    required_classes = {
        "MiniMaxH3HybridRefAndKeyframe",
        "MiniMaxH3TurboLoRA",
        "SamplerCustomAdvanced",
        "H3Idea2VideoProjectImageSave",
        "H3Idea2VideoProjectVideoSave",
    }
    classes = {node.get("class_type") for node in graph.values()}
    for class_type in sorted(required_classes - classes):
        errors.append(f"missing class {class_type}")
    for node_id, node in graph.items():
        if node.get("class_type") != "H3Idea2VideoProjectVideoSave":
            continue
        inputs = node.get("inputs") or {}
        if float(inputs.get("crf", -1)) != 18.0:
            errors.append(f"node {node_id} CRF is not 18")
        relative_path = str(inputs.get("relative_path") or "").replace("\\", "/")
        if not relative_path.endswith(".mp4") or ".." in relative_path.split("/"):
            errors.append(f"node {node_id} project video path invalid")
    for node_id, node in graph.items():
        if node.get("class_type") not in {
            "H3Idea2VideoProjectImageSave",
            "H3Idea2VideoProjectVideoSave",
        }:
            continue
        inputs = node.get("inputs") or {}
        if not str(inputs.get("project_root") or "").strip():
            errors.append(f"node {node_id} project_root missing")
    return errors


def default_template() -> dict[str, Any]:
    value = build_api_workflow(
        prompt=(
            "For the target video, at 0.00 seconds into the target video, "
            "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
            "integrated_multimodal_description: [Shot 1] Paste one validated "
            "Prompt Studio segment here.\n\n"
            "overall_soundscape: Replace with the generated native-audio plan.\n\n"
            "non_diegetic_music: N/A"
        ),
        output_frames=168,
        boundary_before="continuous",
        seed=1,
        run_id="template",
        segment_index=2,
        attempt=1,
        first_frame={"type": "input", "name": "H3Idea2Video/template/handoff/tail_native.png"},
        reference_images=[],
        project_root="$IDEA2VIDEO_PROJECT_ROOT",
    )
    errors = validate_api_workflow(value)
    if errors:
        raise ValueError("; ".join(errors))
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    value = default_template()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Built long-form API workflow: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
