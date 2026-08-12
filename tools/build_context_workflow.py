#!/usr/bin/env python3
"""Build validated API and importable UI workflows for Context Loop sidecars.

This module is offline-only. It never contacts ComfyUI and never queues work.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from context_loop import (  # noqa: E402
    CONTEXT_SCHEMA,
    LOCKED_SETTINGS,
    generation_fingerprint,
)


WORKFLOW_SCHEMA = "h3-prompt-studio-context-loop-api-v1"
UPSCALE_WORKFLOW_SCHEMA = "h3-prompt-studio-context-upscale-api-v1"
UPSCALE_MODEL = "RealESRGAN_x2plus.pth"
PROJECT_ROOT_TOKEN = "$IDEA2VIDEO_PROJECT_ROOT"


def _node(class_type: str, inputs: dict[str, Any], title: str = "") -> dict[str, Any]:
    value: dict[str, Any] = {"class_type": class_type, "inputs": inputs}
    if title:
        value["_meta"] = {"title": title}
    return value


def _project_video_inputs(
    video_link: list[Any], *, project_root: str, relative_path: str
) -> dict[str, Any]:
    return {
        "video": video_link,
        "project_root": project_root,
        "relative_path": relative_path,
        "crf": 18.0,
    }


def build_upscale_api_workflow(
    *,
    source_video: str,
    source_audio: str,
    project_id: str,
    relative_path: str,
    project_root: str = PROJECT_ROOT_TOKEN,
) -> dict[str, Any]:
    """Build one isolated RealESRGAN post-process job for a saved scene."""

    if not str(source_video).strip().endswith("[output]"):
        raise ValueError("source_video must be an annotated ComfyUI output")
    if not str(source_audio).strip().endswith("[output]"):
        raise ValueError("source_audio must be an annotated ComfyUI output")
    if not str(project_id).strip() or not str(relative_path).strip():
        raise ValueError("project_id and relative_path are required")
    graph = {
        "1": _node("LoadVideo", {"file": source_video}),
        "2": _node("GetVideoComponents", {"video": ["1", 0]}),
        "3": _node("LoadAudio", {"audio": source_audio}),
        "4": _node("UpscaleModelLoader", {"model_name": UPSCALE_MODEL}),
        "5": _node(
            "ImageUpscaleWithModel",
            {"upscale_model": ["4", 0], "image": ["2", 0]},
        ),
        "6": _node(
            "ImageScale",
            {
                "image": ["5", 0],
                "upscale_method": "lanczos",
                "width": 1080,
                "height": 1920,
                "crop": "center",
            },
        ),
        "7": _node(
            "CreateVideo",
            {"images": ["6", 0], "audio": ["3", 0], "fps": 24.0, "bit_depth": 10},
        ),
        "8": _node(
            "H3Idea2VideoProjectVideoSave",
            _project_video_inputs(
                ["7", 0], project_root=project_root, relative_path=relative_path
            ),
            "Optional 1080x1920 RealESRGAN scene directly to project",
        ),
    }
    result = {
        "prompt": graph,
        "meta": {
            "schema": UPSCALE_WORKFLOW_SCHEMA,
            "source_video": source_video,
            "source_audio": source_audio,
            "project_id": project_id,
            "project_root": project_root,
            "relative_path": relative_path,
            "model": UPSCALE_MODEL,
            "resolution": [1080, 1920],
            "fps": 24,
            "codec": "h264",
            "crf": 18,
            "output_nodes": {"video": "8"},
        },
    }
    errors = validate_upscale_api_workflow(result)
    if errors:
        raise ValueError("; ".join(errors))
    return result


def validate_upscale_api_workflow(value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    graph = value.get("prompt")
    if not isinstance(graph, dict):
        return ["upscale prompt graph missing"]
    for node_id, node in graph.items():
        for name, item in (node.get("inputs") or {}).items():
            if (
                isinstance(item, list)
                and len(item) == 2
                and isinstance(item[0], str)
                and item[0] not in graph
            ):
                errors.append(f"node {node_id} input {name} is dangling")
    classes = {node.get("class_type") for node in graph.values()}
    required = {
        "LoadVideo",
        "GetVideoComponents",
        "LoadAudio",
        "UpscaleModelLoader",
        "ImageUpscaleWithModel",
        "ImageScale",
        "CreateVideo",
        "H3Idea2VideoProjectVideoSave",
    }
    for name in sorted(required - classes):
        errors.append(f"missing upscale class {name}")
    if (value.get("meta") or {}).get("model") != UPSCALE_MODEL:
        errors.append("upscale model mismatch")
    save = next(
        (
            node
            for node in graph.values()
            if node.get("class_type") == "H3Idea2VideoProjectVideoSave"
        ),
        None,
    )
    if not save or not str(save.get("inputs", {}).get("relative_path") or "").endswith(".mp4"):
        errors.append("upscale output codec mismatch")
    elif float(save["inputs"].get("crf", -1)) != 18.0:
        errors.append("upscale CRF mismatch")
    return errors



def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def plugin_plan(
    spec: dict[str, Any],
    *,
    generation_fingerprint_override: str = "",
    project_root: str = PROJECT_ROOT_TOKEN,
) -> dict[str, Any]:
    """Build the exact plugin-compatible plan used by the project adapter."""

    settings = spec.get("settings") or {}
    compatibility = {
        "fps": int(settings["fps"]),
        "width": int(settings["width"]),
        "height": int(settings["height"]),
        "context_length": int(settings["context_length"]),
        "encode_mode": str(settings["encode_mode"]),
        "anchor_mode": str(settings["anchor_mode"]),
        "crop": str(settings["crop"]),
        "audio_mode": str(settings["audio_mode"]),
        "audio_context_length": int(settings["audio_context_length"]),
        "segment_crf": int(settings["segment_crf"]),
        "generation_fingerprint": (
            generation_fingerprint_override or generation_fingerprint(spec)
        ),
    }
    shots: list[dict[str, Any]] = []
    delivered_before = 0
    for index, scene in enumerate(spec.get("scenes") or [], start=1):
        prompt = str(scene.get("prompt") or "")
        shot = {
            "index": index,
            "id": str(scene["id"]),
            "scene_prompt": prompt,
            "prompt": prompt,
            "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "seed": int(str(scene["seed"])),
            "steps": int(scene["steps"]),
            "raw_frames": int(scene["raw_frames"]),
            "delivered_frames": int(scene["target_frames"]),
            "generation_start_frame": delivered_before,
            "audio_start_seconds": (
                1.0 / 24.0
                if (scene.get("first_frame") or {}).get("source") == "previous_tail"
                else 0.0
            ),
            "audio_duration_seconds": int(scene["raw_frames"]) / 24.0,
            "mode": str(scene["mode"]),
            "boundary_before": str(scene["boundary_before"]),
            "first_frame": copy.deepcopy(scene.get("first_frame")),
            "last_frame": copy.deepcopy(scene.get("last_frame")),
            "target_frames": int(scene["target_frames"]),
        }
        shots.append(shot)
        delivered_before += int(scene["target_frames"])
    plan = {
        "version": 2,
        "project_id": str(spec["project_id"]),
        "project_root": str(project_root),
        "run_name": str(spec["run_name"]),
        "prompt_prefix": "",
        "shots": shots,
        "compatibility": compatibility,
        "segment_crf": int(settings["segment_crf"]),
        "total_delivered_frames": delivered_before,
    }
    plan["plan_hash"] = _stable_hash(
        {
            "compatibility": compatibility,
            "shots": [
                {key: value for key, value in shot.items() if key not in {"prompt", "scene_prompt"}}
                for shot in shots
            ],
        }
    )
    plan["summary"] = (
        f"{len(shots)} clips; {delivered_before} delivered frames "
        f"({delivered_before / 24.0:.3f}s) at {compatibility['width']}x{compatibility['height']}; "
        "exact previous-tail I2VA; deterministic Prompt Studio rule plan"
    )
    return plan


def build_api_workflow(
    spec: dict[str, Any],
    *,
    start_scene: int = 1,
    generation_fingerprint_override: str = "",
    project_root: str = PROJECT_ROOT_TOKEN,
) -> dict[str, Any]:
    if spec.get("schema") != CONTEXT_SCHEMA:
        raise ValueError("unsupported rule workflow spec")
    scenes = spec.get("scenes") or []
    if not scenes:
        raise ValueError("rule workflow spec has no scenes")
    if start_scene < 1 or start_scene > len(scenes):
        raise ValueError("start_scene is outside the scene plan")
    settings = spec.get("settings") or {}
    if settings != LOCKED_SETTINGS:
        raise ValueError("rule workflow settings are not the locked local profile")
    plan = plugin_plan(
        spec,
        generation_fingerprint_override=generation_fingerprint_override,
        project_root=project_root,
    )
    plan_json = json.dumps(plan, ensure_ascii=False, indent=2)
    run_name = str(spec["run_name"])
    graph: dict[str, dict[str, Any]] = {
        "1": _node(
            "UNETLoader",
            {"unet_name": settings["video_model"], "weight_dtype": "default"},
            "MiniMax H3 INT8",
        ),
        "2": _node(
            "MiniMaxH3TurboLoRA",
            {
                "model": ["1", 0],
                "lora_name": settings["turbo_lora"],
                "strength": 1.0,
                "low_vram": False,
            },
            "Turbo v4 / 8 steps",
        ),
        "3": _node(
            "CLIPLoader",
            {"clip_name": settings["text_encoder"], "type": "minimax", "device": "default"},
        ),
        "4": _node("VAELoader", {"vae_name": settings["video_vae"]}),
        "5": _node("VAELoader", {"vae_name": settings["audio_vae"]}),
        "10": _node(
            "H3PromptStudioRulePlan",
            {"plan_json": plan_json},
            "只读规则计划：提示词来自长项目",
        ),
        "11": _node(
            "MiniMaxH3ChainLoopStart",
            {"plan": ["10", 0], "start_clip": int(start_scene), "scene_range": ""},
        ),
        "12": _node("MiniMaxH3ChainCurrent", {"state": ["11", 1]}),
        "20": _node(
            "H3PromptStudioKeyframeRouter",
            {"state": ["12", 0]},
            "上一段精确尾帧 / 已保存 Picture 路由",
        ),
        "30": _node(
            "MiniMaxH3HybridRefAndKeyframe",
            {
                "clip": ["3", 0],
                "vae": ["4", 0],
                "audio_vae": ["5", 0],
                "prompt": ["12", 4],
                "width": ["12", 8],
                "height": ["12", 9],
                "length": ["12", 6],
                "ref_image_size": "match",
                "first_frame": ["20", 0],
                "last_frame": ["20", 1],
                "also_ref_first_frame": False,
            },
            "H3 T2VA / I2VA / FL2VA（按段自动路由）",
        ),
        "40": _node("RandomNoise", {"noise_seed": ["12", 5]}),
        "41": _node(
            "BasicScheduler",
            {"model": ["2", 0], "scheduler": "simple", "steps": ["12", 7], "denoise": 1.0},
        ),
        "42": _node("MiniMaxH3TurboSampler", {}),
        "43": _node("BasicGuider", {"model": ["2", 0], "conditioning": ["30", 0]}),
        "44": _node(
            "SamplerCustomAdvanced",
            {
                "noise": ["40", 0],
                "guider": ["43", 0],
                "sampler": ["42", 0],
                "sigmas": ["41", 0],
                "latent_image": ["30", 1],
            },
        ),
        "45": _node("VAEDecode", {"samples": ["44", 0], "vae": ["4", 0]}),
        "46": _node("VAEDecodeAudio", {"samples": ["44", 0], "vae": ["5", 0]}),
        "47": _node(
            "H3PromptStudioExactFrameMap",
            {
                "state": ["12", 0],
                "images": ["45", 0],
                "audio": ["46", 0],
                "first_frame": ["20", 0],
                "last_frame": ["20", 1],
            },
            "均匀重映射到项目精确帧数并保留端点",
        ),
        "48": _node(
            "MiniMaxH3ChainSegmentSave",
            {
                "state": ["12", 0],
                "images": ["47", 0],
                "sampled_latent": ["44", 0],
                "audio": ["47", 1],
            },
        ),
        "49": _node(
            "MiniMaxH3ChainLoopEnd",
            {
                "flow": ["11", 0],
                "state": ["12", 0],
                "images": ["47", 0],
                "sampled_latent": ["44", 0],
                "segment": ["48", 0],
            },
        ),
        "50": _node(
            "MiniMaxH3ChainAssemble",
            {
                "manifest": ["49", 0],
                "audio_source": "plan",
                "filename": run_name + "_native_768x1344",
                "audio_bitrate": 256,
            },
            "原生总片（保留 H3 生成音频）",
        ),
        "51": _node(
            "H3Idea2VideoProjectFileCopy",
            {
                "source_path": ["50", 0],
                "project_root": project_root,
                "relative_path": (
                    f"{spec['project_id']}/context_loop/output/"
                    f"native_r{int(spec.get('revision') or 0):06d}.mp4"
                ),
            },
            "Copy native master directly to the Idea2Video project",
        ),
    }
    result = {
        "prompt": graph,
        "meta": {
            "schema": "h3-prompt-studio-rule-loop-api-v2",
            "project_id": spec["project_id"],
            "spec_revision": int(spec.get("revision") or 0),
            "run_name": run_name,
            "start_scene": int(start_scene),
            "scene_count": len(scenes),
            "total_delivered_frames": int(spec["total_delivered_frames"]),
            "actual_seconds": float(spec["actual_seconds"]),
            "context_length": 1,
            "continuity": "previous delivered native tail -> next first_frame",
            "audio_mode": settings["audio_mode"],
            "upscale_1080": bool((spec.get("outputs") or {}).get("upscale_1080")),
            "upscale_strategy": "postprocess_each_persisted_scene_then_assemble",
            "prompt_source": "long_project.segments[].h3_prompt",
            "provider_calls": 0,
            "project_root": project_root,
            "output_nodes": {
                "segment_save": "48",
                "assemble_internal": "50",
                "project_native": "51",
            },
            "models": {
                "diffusion": settings["video_model"],
                "lora": settings["turbo_lora"],
                "steps": settings["steps"],
                "native_resolution": [settings["width"], settings["height"]],
                "fps": settings["fps"],
                "codec": "h264",
                "crf": settings["segment_crf"],
            },
        },
    }
    errors = validate_api_workflow(result)
    if errors:
        raise ValueError("; ".join(errors))
    return result


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
    classes = [node.get("class_type") for node in graph.values()]
    required = {
        "H3PromptStudioRulePlan",
        "H3PromptStudioKeyframeRouter",
        "H3PromptStudioExactFrameMap",
        "MiniMaxH3ChainLoopStart",
        "MiniMaxH3ChainCurrent",
        "MiniMaxH3HybridRefAndKeyframe",
        "MiniMaxH3ChainSegmentSave",
        "MiniMaxH3ChainLoopEnd",
        "MiniMaxH3ChainAssemble",
        "MiniMaxH3TurboLoRA",
        "MiniMaxH3TurboSampler",
        "H3Idea2VideoProjectFileCopy",
    }
    for class_type in sorted(required - set(classes)):
        errors.append(f"missing class {class_type}")
    if "MiniMaxH3ChainReview" in classes:
        errors.append("Review Gate must not block unattended rendering")
    for forbidden in (
        "MiniMaxH3ChainPlan",
        "MiniMaxH3ChainScenePromptEditor",
        "MiniMaxH3ChainContext",
        "MiniMaxH3LoopTrim",
    ):
        if forbidden in classes:
            errors.append(f"rule workflow must not contain {forbidden}")
    if classes.count("MiniMaxH3ChainSegmentSave") != 1:
        errors.append("workflow must contain one Segment Save")
    if classes.count("MiniMaxH3ChainLoopEnd") != 1:
        errors.append("workflow must contain one Loop End")
    plan_nodes = [node for node in graph.values() if node.get("class_type") == "H3PromptStudioRulePlan"]
    if len(plan_nodes) == 1:
        plan_inputs = plan_nodes[0]["inputs"]
        try:
            plan_value = json.loads(plan_inputs.get("plan_json") or "")
            shots = plan_value.get("shots") or []
            if not shots:
                errors.append("plugin plan has no shots")
            compatibility = plan_value.get("compatibility") or {}
            if int(compatibility.get("context_length", -1)) != 1:
                errors.append("context_length is not exact-tail 1")
            if compatibility.get("audio_mode") != "generated_audio":
                errors.append("audio mode is not generated_audio")
            for shot in shots:
                length = int(shot.get("raw_frames") or 0)
                if length % 17 != 5:
                    errors.append(f"shot {shot.get('id')} length is not 17k+5")
                if int(shot.get("steps") or 0) != 8:
                    errors.append(f"shot {shot.get('id')} steps is not 8")
                if int(shot.get("delivered_frames") or 0) != int(shot.get("target_frames") or -1):
                    errors.append(f"shot {shot.get('id')} delivered frame mismatch")
                if str(shot.get("prompt") or "") != str(shot.get("scene_prompt") or ""):
                    errors.append(f"shot {shot.get('id')} prompt was transformed")
        except (TypeError, ValueError, json.JSONDecodeError):
            errors.append("plugin plan_json invalid")
    meta = value.get("meta") or {}
    models = meta.get("models") or {}
    if models.get("diffusion") != LOCKED_SETTINGS["video_model"]:
        errors.append("diffusion model mismatch")
    if models.get("lora") != LOCKED_SETTINGS["turbo_lora"]:
        errors.append("Turbo LoRA mismatch")
    if int(models.get("steps") or 0) != 8:
        errors.append("meta steps mismatch")
    output_nodes = meta.get("output_nodes") or {}
    for name in ("segment_save", "assemble_internal", "project_native"):
        if str(output_nodes.get(name) or "") not in graph:
            errors.append(f"missing output node {name}")
    hybrid = next(
        (node for node in graph.values() if node.get("class_type") == "MiniMaxH3HybridRefAndKeyframe"),
        None,
    )
    if hybrid:
        if hybrid["inputs"].get("first_frame") != ["20", 0]:
            errors.append("first-frame router connection mismatch")
        if hybrid["inputs"].get("last_frame") != ["20", 1]:
            errors.append("last-frame router connection mismatch")
        if bool(hybrid["inputs"].get("also_ref_first_frame")):
            errors.append("rule workflow must not duplicate first frame as Ref2VA input")
    mapper = next(
        (node for node in graph.values() if node.get("class_type") == "H3PromptStudioExactFrameMap"),
        None,
    )
    if mapper and mapper.get("inputs", {}).get("images") != ["45", 0]:
        errors.append("exact frame mapper is not connected to native decoded frames")
    return errors


NODE_DEFS: dict[str, dict[str, Any]] = {
    "UNETLoader": {"inputs": [("unet_name", "COMBO", True), ("weight_dtype", "COMBO", True)], "outputs": [("MODEL", "MODEL")]},
    "MiniMaxH3TurboLoRA": {"inputs": [("model", "MODEL", False), ("lora_name", "COMBO", True), ("strength", "FLOAT", True), ("low_vram", "BOOLEAN", True)], "outputs": [("MODEL", "MODEL")]},
    "CLIPLoader": {"inputs": [("clip_name", "COMBO", True), ("type", "COMBO", True), ("device", "COMBO", True)], "outputs": [("CLIP", "CLIP")]},
    "VAELoader": {"inputs": [("vae_name", "COMBO", True)], "outputs": [("VAE", "VAE")]},
    "H3PromptStudioRulePlan": {"inputs": [("plan_json", "STRING", True)], "outputs": [("plan", "H3_CHAIN_PLAN"), ("summary", "STRING"), ("clip_count", "INT"), ("width", "INT"), ("height", "INT")]},
    "H3PromptStudioKeyframeRouter": {"inputs": [("state", "H3_CHAIN_STATE", False)], "outputs": [("first_frame", "IMAGE"), ("last_frame", "IMAGE"), ("status", "STRING")]},
    "H3PromptStudioExactFrameMap": {"inputs": [("state", "H3_CHAIN_STATE", False), ("images", "IMAGE", False), ("audio", "AUDIO", False), ("first_frame", "IMAGE", False), ("last_frame", "IMAGE", False)], "outputs": [("images", "IMAGE"), ("audio", "AUDIO"), ("status", "STRING")]},
    "MiniMaxH3ChainPlan": {"inputs": [(name, typ, True) for name, typ in [("plan_json", "STRING"), ("run_name", "STRING"), ("generation_fingerprint", "STRING"), ("width", "INT"), ("height", "INT"), ("context_length", "COMBO"), ("encode_mode", "COMBO"), ("anchor_mode", "COMBO"), ("crop", "COMBO"), ("audio_mode", "COMBO"), ("audio_context_length", "INT"), ("default_duration_seconds", "FLOAT"), ("default_steps", "INT"), ("base_seed", "INT"), ("segment_crf", "INT")]], "outputs": [("plan", "H3_CHAIN_PLAN"), ("summary", "STRING"), ("clip_count", "INT"), ("width", "INT"), ("height", "INT")]},
    "MiniMaxH3ChainScenePromptEditor": {"inputs": [("plan", "H3_CHAIN_PLAN", False)], "outputs": [("plan", "H3_CHAIN_PLAN")]},
    "MiniMaxH3ChainLoopStart": {"inputs": [("plan", "H3_CHAIN_PLAN", False), ("start_clip", "INT", True), ("scene_range", "STRING", True)], "outputs": [("flow", "H3_CHAIN_FLOW"), ("state", "H3_CHAIN_STATE"), ("status", "STRING")]},
    "MiniMaxH3ChainCurrent": {"inputs": [("state", "H3_CHAIN_STATE", False)], "outputs": [("state", "H3_CHAIN_STATE"), ("clip_index", "INT"), ("clip_count", "INT"), ("shot_id", "STRING"), ("prompt", "STRING"), ("noise_seed", "INT"), ("length", "INT"), ("steps", "INT"), ("width", "INT"), ("height", "INT"), ("audio_start", "FLOAT"), ("audio_duration", "FLOAT"), ("source_audio_slice", "AUDIO"), ("status", "STRING")]},
    "LoadImage": {"inputs": [("image", "COMBO", True)], "outputs": [("IMAGE", "IMAGE"), ("MASK", "MASK")]},
    "MiniMaxH3ChainFirstSceneImage": {"inputs": [("state", "H3_CHAIN_STATE", False), ("image", "IMAGE", False)], "outputs": [("first_frame", "IMAGE"), ("is_first_scene", "BOOLEAN"), ("status", "STRING")]},
    "MiniMaxH3HybridRefAndKeyframe": {"inputs": [("clip", "CLIP", False), ("vae", "VAE", False), ("audio_vae", "VAE", False), ("prompt", "STRING", True), ("width", "INT", True), ("height", "INT", True), ("length", "INT", True), ("ref_image_size", "COMBO", True), ("first_frame", "IMAGE", False), ("last_frame", "IMAGE", False), ("also_ref_first_frame", "BOOLEAN", True)], "outputs": [("positive", "CONDITIONING"), ("LATENT", "LATENT")]},
    "MiniMaxH3PatchPriority": {"inputs": [("conditioning", "CONDITIONING", False)], "outputs": [("conditioning", "CONDITIONING"), ("status", "STRING")]},
    "MiniMaxH3ChainContext": {"inputs": [("state", "H3_CHAIN_STATE", False), ("conditioning", "CONDITIONING", False), ("vae", "VAE", False), ("latent", "LATENT", False), ("audio_vae", "VAE", False)], "outputs": [("conditioning", "CONDITIONING"), ("trim_frames", "INT"), ("is_continuation", "BOOLEAN")]},
    "RandomNoise": {"inputs": [("noise_seed", "INT", True)], "outputs": [("NOISE", "NOISE")]},
    "BasicScheduler": {"inputs": [("model", "MODEL", False), ("scheduler", "COMBO", True), ("steps", "INT", True), ("denoise", "FLOAT", True)], "outputs": [("SIGMAS", "SIGMAS")]},
    "MiniMaxH3TurboSampler": {"inputs": [], "outputs": [("SAMPLER", "SAMPLER")]},
    "BasicGuider": {"inputs": [("model", "MODEL", False), ("conditioning", "CONDITIONING", False)], "outputs": [("GUIDER", "GUIDER")]},
    "SamplerCustomAdvanced": {"inputs": [("noise", "NOISE", False), ("guider", "GUIDER", False), ("sampler", "SAMPLER", False), ("sigmas", "SIGMAS", False), ("latent_image", "LATENT", False)], "outputs": [("output", "LATENT"), ("denoised_output", "LATENT")]},
    "VAEDecode": {"inputs": [("samples", "LATENT", False), ("vae", "VAE", False)], "outputs": [("IMAGE", "IMAGE")]},
    "VAEDecodeAudio": {"inputs": [("samples", "LATENT", False), ("vae", "VAE", False)], "outputs": [("AUDIO", "AUDIO")]},
    "MiniMaxH3LoopTrim": {"inputs": [("images", "IMAGE", False), ("trim_frames", "INT", True), ("audio", "AUDIO", False), ("fps", "FLOAT", True), ("match_tail", "BOOLEAN", True)], "outputs": [("images", "IMAGE"), ("audio", "AUDIO")]},
    "MiniMaxH3ChainSegmentSave": {"inputs": [("state", "H3_CHAIN_STATE", False), ("images", "IMAGE", False), ("sampled_latent", "LATENT", False), ("audio", "AUDIO", False)], "outputs": [("segment", "H3_CHAIN_SEGMENT"), ("status", "STRING")]},
    "MiniMaxH3ChainLoopEnd": {"inputs": [("flow", "H3_CHAIN_FLOW", False), ("state", "H3_CHAIN_STATE", False), ("images", "IMAGE", False), ("sampled_latent", "LATENT", False), ("segment", "H3_CHAIN_SEGMENT", False)], "outputs": [("manifest", "H3_CHAIN_MANIFEST"), ("manifest_json", "STRING"), ("last_context_frames", "IMAGE"), ("last_context_latent", "LATENT")]},
    "MiniMaxH3ChainAssemble": {"inputs": [("manifest", "H3_CHAIN_MANIFEST", False), ("audio_source", "COMBO", True), ("filename", "STRING", True), ("audio_bitrate", "INT", True)], "outputs": [("video_path", "STRING")]},
    "H3Idea2VideoProjectFileCopy": {"inputs": [("source_path", "STRING", False), ("project_root", "STRING", True), ("relative_path", "STRING", True)], "outputs": [("receipt_json", "STRING")]},
    "H3Idea2VideoProjectVideoSave": {"inputs": [("video", "VIDEO", False), ("project_root", "STRING", True), ("relative_path", "STRING", True), ("crf", "FLOAT", True)], "outputs": [("receipt_json", "STRING")]},
}


def _hybrid_inputs(graph_node: dict[str, Any]) -> list[tuple[str, str, bool]]:
    base = list(NODE_DEFS["MiniMaxH3HybridRefAndKeyframe"]["inputs"])
    insert_at = next(index for index, item in enumerate(base) if item[0] == "also_ref_first_frame")
    dynamic = sorted(
        [name for name in graph_node["inputs"] if name.startswith("ref_images.ref_image_")],
        key=lambda name: int(name.rsplit("_", 1)[-1]),
    )
    for offset, name in enumerate(dynamic):
        base.insert(insert_at + offset, (name, "IMAGE", False))
    return base


def build_ui_workflow(api_workflow: dict[str, Any]) -> dict[str, Any]:
    errors = validate_api_workflow(api_workflow)
    if errors:
        raise ValueError("; ".join(errors))
    graph = api_workflow["prompt"]
    ordered_ids = sorted(graph, key=lambda value: int(value))
    nodes: dict[str, dict[str, Any]] = {}
    input_defs: dict[str, list[tuple[str, str, bool]]] = {}
    for order, node_id in enumerate(ordered_ids):
        graph_node = graph[node_id]
        class_type = graph_node["class_type"]
        definition = NODE_DEFS.get(class_type)
        if definition is None:
            raise ValueError(f"UI definition missing for {class_type}")
        defs = _hybrid_inputs(graph_node) if class_type == "MiniMaxH3HybridRefAndKeyframe" else list(definition["inputs"])
        input_defs[node_id] = defs
        inputs = []
        widgets = []
        for name, type_name, widget in defs:
            item: dict[str, Any] = {"name": name, "type": type_name, "link": None}
            if widget:
                item["widget"] = {"name": name}
                value = graph_node["inputs"].get(name)
                if isinstance(value, list) and len(value) == 2:
                    value = {"STRING": "", "INT": 0, "FLOAT": 0.0, "BOOLEAN": False}.get(type_name, "")
                widgets.append(value)
            inputs.append(item)
        outputs = [
            {"name": name, "type": type_name, "links": None}
            for name, type_name in definition["outputs"]
        ]
        column = order % 6
        row = order // 6
        nodes[node_id] = {
            "id": int(node_id),
            "type": class_type,
            "pos": [80 + column * 430, 80 + row * 340],
            "size": [380, 520 if class_type == "H3PromptStudioRulePlan" else 250],
            "flags": {},
            "order": order,
            "mode": 0,
            "inputs": inputs,
            "outputs": outputs,
            "title": (graph_node.get("_meta") or {}).get("title", ""),
            "properties": {"Node name for S&R": class_type},
            "widgets_values": widgets,
        }

    links: list[list[Any]] = []
    link_id = 0
    for target_id in ordered_ids:
        graph_node = graph[target_id]
        defs = input_defs[target_id]
        slot_by_name = {name: index for index, (name, _type, _widget) in enumerate(defs)}
        type_by_name = {name: type_name for name, type_name, _widget in defs}
        for name, value in graph_node["inputs"].items():
            if not (
                isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], str)
                and isinstance(value[1], int)
            ):
                continue
            if name not in slot_by_name:
                raise ValueError(f"UI input definition missing for {target_id}.{name}")
            source_id, source_slot = value
            if source_id not in nodes:
                raise ValueError(f"UI link source missing: {source_id}")
            link_id += 1
            target_slot = slot_by_name[name]
            link_type = type_by_name[name]
            links.append(
                [
                    link_id,
                    int(source_id),
                    int(source_slot),
                    int(target_id),
                    target_slot,
                    link_type,
                ]
            )
            nodes[target_id]["inputs"][target_slot]["link"] = link_id
            source_output = nodes[source_id]["outputs"][source_slot]
            source_output["links"] = list(source_output.get("links") or []) + [link_id]

    stable_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        _canonical_for_id(api_workflow.get("meta") or {}),
    )
    workflow = {
        "id": str(stable_id),
        "revision": 0,
        "last_node_id": max(int(value) for value in nodes),
        "last_link_id": link_id,
        "nodes": [nodes[node_id] for node_id in ordered_ids],
        "links": links,
        "groups": [],
        "config": {},
        "extra": {
            "h3_prompt_studio": copy.deepcopy(api_workflow.get("meta") or {}),
            "ds": {"scale": 0.7, "offset": [40, 40]},
        },
        "version": 0.4,
    }
    ui_errors = validate_ui_workflow(workflow)
    if ui_errors:
        raise ValueError("; ".join(ui_errors))
    return workflow


def _canonical_for_id(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_ui_workflow(workflow: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    nodes = workflow.get("nodes") or []
    links = workflow.get("links") or []
    node_map = {int(node.get("id")): node for node in nodes if isinstance(node, dict)}
    link_map = {int(link[0]): link for link in links if isinstance(link, list) and len(link) >= 6}
    if len(node_map) != len(nodes):
        errors.append("UI node IDs are invalid or duplicated")
    for link_id, link in link_map.items():
        _id, source, source_slot, target, target_slot, _type = link
        if int(source) not in node_map or int(target) not in node_map:
            errors.append(f"UI link {link_id} has missing endpoint")
            continue
        if int(source_slot) >= len(node_map[int(source)].get("outputs") or []):
            errors.append(f"UI link {link_id} source slot invalid")
        if int(target_slot) >= len(node_map[int(target)].get("inputs") or []):
            errors.append(f"UI link {link_id} target slot invalid")
    for node in nodes:
        for item in node.get("inputs") or []:
            if item.get("link") is not None and int(item["link"]) not in link_map:
                errors.append(f"UI node {node.get('id')} has dangling input link")
        for item in node.get("outputs") or []:
            for linked in item.get("links") or []:
                if int(linked) not in link_map:
                    errors.append(f"UI node {node.get('id')} has dangling output link")
    return errors


def write_artifacts(spec: dict[str, Any], destination: Path) -> dict[str, Path]:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    api = build_api_workflow(spec)
    ui = build_ui_workflow(api)
    plan = plugin_plan(spec)
    outputs = {
        "plan": destination / "plan.json",
        "api_prompt": destination / "api_prompt.json",
        "workflow": destination / "workflow.json",
    }
    documents = {"plan": plan, "api_prompt": api, "workflow": ui}
    for name, path in outputs.items():
        path.write_text(
            json.dumps(documents[name], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    for name, path in write_artifacts(spec, args.output).items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
