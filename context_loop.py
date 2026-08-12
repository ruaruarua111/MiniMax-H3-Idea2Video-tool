"""Validated sidecar state for the optional MiniMax H3 Context Loop mode.

The existing long-project schema deliberately does not import this module.  A
context plan lives beside ``project.json`` and can be removed without changing
or migrating the stable long-form project.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import secrets
from pathlib import Path
from typing import Any

from longform import (
    FPS,
    LongFormError,
    LongProjectStore,
    content_sync_is_current,
    h3_model_frames,
    merge_usage,
    utc_now,
)


CONTEXT_SCHEMA = "context_workflow_spec_v1"
CONTEXT_SCHEMA_VERSION = 2
CONTEXT_LENGTH = 1
AUDIO_CONTEXT_LENGTH = 1
MIN_SUGGESTED_SECONDS = 4.0
MAX_SUGGESTED_SECONDS = 15.0
MIN_RELIABLE_RAW_FRAMES = 124
MAX_SHOTS = 128
MAX_IDENTITY_IMAGES = 8
MAX_SEED = 0xFFFFFFFFFFFFFFFF
H3_MODULUS = 17
H3_REMAINDER = 5
I2VA_LINE = (
    "For the target video, at 0.00 seconds into the target video, "
    "<Picture 1> (from [Shot 1]) is fully referenced."
)
BASE_FIELDS = (
    "integrated_multimodal_description",
    "overall_soundscape",
    "non_diegetic_music",
)
REF_FIELDS = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)
LOCKED_SETTINGS = {
    "width": 768,
    "height": 1344,
    "fps": FPS,
    "steps": 8,
    "context_length": CONTEXT_LENGTH,
    "audio_context_length": AUDIO_CONTEXT_LENGTH,
    "encode_mode": "frames",
    "anchor_mode": "before",
    "crop": "disabled",
    "audio_mode": "generated_audio",
    "segment_crf": 18,
    "video_model": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "turbo_lora": "minimax_h3_turbo_v4_step600_ema.safetensors",
    "text_encoder": "qwen3vl_32b_minimax_h3_bf16.safetensors",
    "video_vae": "minimax_h3_video_vae_fp16.safetensors",
    "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _clean_text(value: Any, *, limit: int = 200_000) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > limit:
        raise LongFormError("Context Loop 文本过长。", "context_text_too_large")
    return text


def _safe_run_name(project_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", str(project_id)).strip("._-")
    return ("h3ps_" + (value or "context"))[:88]


def _normalize_input_name(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise LongFormError("参考图必须位于 ComfyUI input。", "context_reference_invalid")
    parts = [part for part in text.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise LongFormError("参考图路径无效。", "context_reference_invalid")
    return "/".join(parts)


def _segment_source_document(segment: dict[str, Any]) -> dict[str, Any]:
    workspace = segment.get("single_workspace") or {}
    return {
        "id": str(segment.get("id") or ""),
        "index": int(segment.get("index") or 0),
        "frames": int(segment.get("frames") or 0),
        "boundary_before": str(segment.get("boundary_before") or ""),
        "story_card": copy.deepcopy(segment.get("story_card") or {}),
        "script": copy.deepcopy(workspace.get("script") or {}),
        "summary": str(segment.get("summary") or ""),
        "visual": str(segment.get("visual") or ""),
        "beats": copy.deepcopy(segment.get("beats") or []),
        "camera": str(segment.get("camera") or ""),
        "dialogue": copy.deepcopy(segment.get("dialogue") or []),
        "visible_text": copy.deepcopy(segment.get("visible_text") or []),
        "sound": str(segment.get("sound") or ""),
        "music": str(segment.get("music") or ""),
        "continuity_in": str(segment.get("continuity_in") or ""),
        "continuity_out": str(segment.get("continuity_out") or ""),
        "extra_constraints": str(segment.get("extra_constraints") or ""),
        "h3_prompt": str(segment.get("h3_prompt") or ""),
        "prompt_state": str(segment.get("prompt_state") or ""),
        "content_sync": copy.deepcopy(segment.get("content_sync") or {}),
    }


def segment_source_hash(segment: dict[str, Any]) -> str:
    return _hash(_segment_source_document(segment))


def project_source_fingerprint(project: dict[str, Any]) -> str:
    return _hash(
        {
            "id": project.get("id"),
            "title": project.get("title"),
            "idea": project.get("idea"),
            "story_bible": project.get("story_bible") or {},
            "outline": project.get("outline") or [],
            "ending_requirements": project.get("ending_requirements") or [],
            "initial_frame": project.get("initial_frame"),
            "user_identity_references": project.get("user_identity_references") or [],
            "reference_analysis": project.get("reference_analysis") or [],
            "segments": [
                _segment_source_document(item) for item in project.get("segments") or []
            ],
        }
    )


def reference_spec(
    project: dict[str, Any],
    *,
    opening_image: Any = None,
    identity_images: Any = None,
) -> dict[str, Any]:
    descriptions = {
        str(item.get("source_name") or ""): str(item.get("description") or "")
        for item in project.get("reference_analysis") or []
        if isinstance(item, dict)
    }
    if opening_image is None:
        opening_image = project.get("initial_frame")
    if isinstance(opening_image, dict):
        opening_name = _normalize_input_name(opening_image.get("name"))
        opening_description = str(
            opening_image.get("description") or descriptions.get(opening_name, "")
        ).strip()
    else:
        opening_name = _normalize_input_name(opening_image)
        opening_description = descriptions.get(opening_name, "").strip()
    opening = None
    if opening_name:
        opening = {
            "type": "input",
            "name": opening_name,
            "description": opening_description,
        }

    if identity_images is None:
        identity_images = project.get("user_identity_references") or []
    if not isinstance(identity_images, list):
        raise LongFormError("身份参考图必须是数组。", "context_reference_invalid")
    identities: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in identity_images:
        if isinstance(item, dict):
            name = _normalize_input_name(item.get("name"))
            description = str(
                item.get("description") or descriptions.get(name, "")
            ).strip()
        else:
            name = _normalize_input_name(item)
            description = descriptions.get(name, "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        identities.append(
            {"type": "input", "name": name, "description": description}
        )
    if opening_name and any(item["name"] == opening_name for item in identities):
        raise LongFormError(
            "同一张图片不能同时作为开场图和身份参考图。",
            "context_reference_duplicate_role",
        )
    if len(identities) > MAX_IDENTITY_IMAGES:
        raise LongFormError(
            f"高级 Context Loop 身份参考图最多 {MAX_IDENTITY_IMAGES} 张。",
            "context_reference_count_invalid",
        )
    return {"opening_image": opening, "identity_images": identities}


def _valid_h3_frame(value: int) -> bool:
    return value >= 5 and value % H3_MODULUS == H3_REMAINDER


def calibrate_scene_duration(
    suggested_seconds: Any,
    *,
    scene_index: int,
    context_length: int = CONTEXT_LENGTH,
) -> dict[str, Any]:
    try:
        seconds = float(suggested_seconds)
    except (TypeError, ValueError) as exc:
        raise LongFormError("建议时长必须是数字。", "context_duration_invalid") from exc
    if not math.isfinite(seconds) or not (
        MIN_SUGGESTED_SECONDS <= seconds <= MAX_SUGGESTED_SECONDS
    ):
        raise LongFormError(
            "高级模式每段建议时长必须在 4–15 秒之间。",
            "context_duration_invalid",
        )
    desired_delivered = max(1, int(math.floor(seconds * FPS + 0.5)))
    target_raw = desired_delivered + (context_length if scene_index > 1 else 0)
    target_raw = max(MIN_RELIABLE_RAW_FRAMES, target_raw)
    lower_k = math.floor((target_raw - H3_REMAINDER) / H3_MODULUS)
    candidates = {
        H3_REMAINDER + H3_MODULUS * max(0, lower_k - 1),
        H3_REMAINDER + H3_MODULUS * max(0, lower_k),
        H3_REMAINDER + H3_MODULUS * max(0, lower_k + 1),
        H3_REMAINDER + H3_MODULUS * max(0, lower_k + 2),
        MIN_RELIABLE_RAW_FRAMES,
    }
    candidates = {
        value
        for value in candidates
        if value >= MIN_RELIABLE_RAW_FRAMES and _valid_h3_frame(value)
    }
    if not candidates:
        raise AssertionError("no H3 duration candidates")

    def delivered(raw: int) -> int:
        return raw - context_length if scene_index > 1 else raw

    raw_frames = min(
        candidates,
        key=lambda value: (abs(delivered(value) - desired_delivered), -value),
    )
    delivered_frames = delivered(raw_frames)
    if delivered_frames <= 0:
        raise LongFormError("校准后的交付帧数无效。", "context_duration_invalid")
    return {
        "suggested_duration_seconds": round(seconds, 6),
        "desired_delivered_frames": desired_delivered,
        "raw_frames": raw_frames,
        "delivered_frames": delivered_frames,
        "actual_seconds": delivered_frames / FPS,
    }


def derive_scene_seed(base_seed: int, index: int, scene_id: str) -> int:
    payload = f"{int(base_seed)}:{int(index)}:{scene_id}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _dialogue_requirements(segment: dict[str, Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for item in segment.get("dialogue") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        language = str(item.get("language") or "Chinese")
        if text:
            result.append((language, text))
    return result


def _field_positions(text: str, fields: tuple[str, ...]) -> list[int]:
    return [text.find(field + ":") for field in fields]


def validate_scene_prompt(
    prompt: str,
    *,
    segment: dict[str, Any],
    scene_index: int,
    actual_seconds: float,
    identity_count: int,
    has_opening_image: bool,
) -> list[str]:
    text = _clean_text(prompt)
    if not text:
        return ["完整 H3 提示词为空。"]
    errors: list[str] = []
    if identity_count:
        positions = _field_positions(text, REF_FIELDS)
        if any(value < 0 for value in positions) or positions != sorted(positions):
            errors.append("Ref2VA 六个字段缺失或顺序错误。")
        if any(text.count(field + ":") != 1 for field in REF_FIELDS):
            errors.append("Ref2VA 六个字段必须各出现一次。")
        if "integrated_multimodal_description:" in text:
            errors.append("Ref2VA 提示词不得包含基础 integrated 字段。")
        allowed = identity_count + (1 if scene_index == 1 and has_opening_image else 0)
        picture_numbers = [
            int(value) for value in re.findall(r"<Picture\s+(\d+)>", text)
        ]
        if any(number < 1 or number > allowed for number in picture_numbers):
            errors.append("提示词包含未连接的 Picture 引用。")
        subject_end = positions[1] if len(positions) > 1 and positions[1] >= 0 else len(text)
        definitions = text[:subject_end]
        for number in range(1, allowed + 1):
            if f"<Picture {number}>" not in definitions:
                errors.append(f"subject_definitions 未定义 Picture {number}。")
        all_subjects = {int(v) for v in re.findall(r"<Subject\s+(\d+)>", text)}
        defined_subjects = {
            int(v) for v in re.findall(r"<Subject\s+(\d+)>", definitions)
        }
        if any(number < 1 for number in all_subjects):
            errors.append("Subject 编号必须从 1 开始。")
        elif not all_subjects.issubset(defined_subjects):
            errors.append("提示词引用了未在 subject_definitions 定义的 Subject。")
        if re.search(r"<(?:Video|Audio)\s+\d+>", text):
            errors.append("当前高级模式未连接 Video 或 Audio 参考。")
        if scene_index > 1 and has_opening_image and f"<Picture {identity_count + 1}>" in text:
            errors.append("首图只能在第一段引用。")
    else:
        positions = _field_positions(text, BASE_FIELDS)
        if any(value < 0 for value in positions) or positions != sorted(positions):
            errors.append("三个基础字段缺失或顺序错误。")
        if any(text.count(field + ":") != 1 for field in BASE_FIELDS):
            errors.append("三个基础字段必须各出现一次。")
        for field in REF_FIELDS[:4]:
            if field + ":" in text:
                errors.append("基础模式不得包含 Ref2VA 六段式字段。")
                break
        if scene_index == 1 and has_opening_image:
            if not text.startswith(I2VA_LINE + "\n\n"):
                errors.append("第一段缺少官方 I2VA 首行或空行。")
        else:
            if (
                text.startswith("For the target video")
                or "reference pictures align" in text[:400]
                or re.search(r"<Picture\s+\d+>", text)
            ):
                errors.append("Context 后续段不得伪造 Picture 对齐或引用。")
        if re.search(r"<(?:Subject|Video|Audio)\s+\d+>", text):
            errors.append("基础模式不得引用 Subject、Video 或 Audio。")

    for language, dialogue in _dialogue_requirements(segment):
        exact = f"<d>[{language}] {dialogue}</d>"
        count = text.count(exact)
        if count != 1:
            errors.append(f"精确对白必须逐字保留且只出现一次：{dialogue}")
    for visible in segment.get("visible_text") or []:
        visible_text = str(visible)
        if visible_text and visible_text not in text:
            errors.append(f"可见文字未完整保留：{visible_text}")
    if text.count("<d>") != text.count("</d>"):
        errors.append("对白 <d> 标签不完整。")
    for block in re.findall(r"<d>(.*?)</d>", text, flags=re.DOTALL):
        if not re.match(r"\[[A-Za-z][A-Za-z -]*] ", block):
            errors.append("对白缺少完整语言标签。")
            break
    timing_text = text
    for _language, dialogue in _dialogue_requirements(segment):
        timing_text = timing_text.replace(dialogue, "")
    for visible in segment.get("visible_text") or []:
        timing_text = timing_text.replace(str(visible), "")
    for minutes, seconds in re.findall(r"(\d{2}):(\d{2}(?:\.\d{1,3})?)", timing_text):
        if int(minutes) * 60 + float(seconds) > actual_seconds + 1 / FPS:
            errors.append("提示词时间戳超过校准后的分段时长。")
            break
    if not errors:
        for seconds in re.findall(
            r"(?<![\d:])(\d+(?:\.\d{1,3})?)\s*(?:-second\s+mark|seconds?\s+into)",
            timing_text,
            flags=re.IGNORECASE,
        ):
            if float(seconds) > actual_seconds + 1 / FPS:
                errors.append("提示词时间戳超过校准后的分段时长。")
                break
    return errors


def normalize_generated_scene(
    raw: Any,
    *,
    segment: dict[str, Any],
    scene_index: int,
    base_seed: int,
    identity_count: int,
    has_opening_image: bool,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LongFormError("Qwen 场景结果必须是 JSON 对象。", "context_scene_invalid")
    expected_id = str(segment.get("id") or f"seg_{scene_index:04d}")
    if str(raw.get("id") or "") != expected_id:
        raise LongFormError("Qwen 改变了场景 ID。", "context_scene_id_mismatch")
    prompt = _clean_text(raw.get("prompt"))
    timing = calibrate_scene_duration(
        raw.get("suggested_duration_seconds", segment.get("duration_seconds")),
        scene_index=scene_index,
    )
    errors = validate_scene_prompt(
        prompt,
        segment=segment,
        scene_index=scene_index,
        actual_seconds=timing["actual_seconds"],
        identity_count=identity_count,
        has_opening_image=has_opening_image,
    )
    if errors:
        error = LongFormError("；".join(errors), "context_prompt_invalid")
        error.validation_errors = errors  # type: ignore[attr-defined]
        error.timing = timing  # type: ignore[attr-defined]
        raise error
    warnings = raw.get("warnings") or []
    if not isinstance(warnings, list):
        warnings = [str(warnings)]
    return {
        "id": expected_id,
        "source_segment_id": expected_id,
        "source_index": scene_index,
        "source_hash": segment_source_hash(segment),
        "prompt": prompt,
        **timing,
        "steps": LOCKED_SETTINGS["steps"],
        "seed": str(derive_scene_seed(base_seed, scene_index, expected_id)),
        "continuity_summary": _clean_text(raw.get("continuity_summary"), limit=20_000),
        "warnings": [str(item) for item in warnings if str(item)],
        "provenance": "qwen",
        "manual_revision": 0,
    }


def _saved_input_picture(
    segment: dict[str, Any],
    slot: str,
    *,
    required: bool,
) -> dict[str, Any] | None:
    workspace = segment.get("single_workspace") or {}
    picture = (workspace.get("pictures") or {}).get(slot) or {}
    source = str(picture.get("source") or "none")
    if source == "project_asset":
        asset_id = str(picture.get("asset_id") or "")
        relative_path = str(picture.get("relative_path") or "")
        if re.fullmatch(r"[0-9a-f]{64}", asset_id) and relative_path:
            return {
                "source": "project_asset",
                "asset_id": asset_id,
                "relative_path": relative_path,
                "mime": str(picture.get("mime") or ""),
                "original_name": str(picture.get("original_name") or ""),
                "name": "",
            }
    if source == "temporary":
        raise LongFormError(
            f"第 {segment['index']} 段 {slot} 仍是浏览器临时图片；请先保存本段，让图片进入项目 assets。",
            "context_temporary_picture_unsupported",
        )
    if source == "input":
        name = _normalize_input_name(picture.get("input_path"))
        if name:
            return {"source": "input", "name": name}
    if required:
        raise LongFormError(
            f"第 {segment['index']} 段 {slot} 缺少已保存的项目图片。",
            "context_picture_missing",
        )
    return None


def build_rule_spec(
    project: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
    base_seed: int | None = None,
    upscale_1080: bool | None = None,
) -> dict[str, Any]:
    """Compile one existing long project into an immutable rule-execution spec.

    This function never calls a model. Every scene prompt is copied byte-for-
    byte from ``segments[].h3_prompt`` and every picture route comes from the
    saved single-workspace state.
    """

    segments = project.get("segments") or []
    if not segments:
        raise LongFormError("长视频项目没有分段。", "context_project_invalid")
    if len(segments) > MAX_SHOTS:
        raise LongFormError(
            f"规则工作流最多支持 {MAX_SHOTS} 段。", "context_scene_count_invalid"
        )
    prior = existing or {}
    seed_value = (
        base_seed
        if base_seed is not None
        else int(str(prior.get("base_seed") or secrets.randbits(64)))
    )
    seed = int(seed_value)
    if seed < 0 or seed > MAX_SEED:
        raise LongFormError("基础 Seed 超出范围。", "context_seed_invalid")

    scenes: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, segment in enumerate(segments, start=1):
        sync_state = str((segment.get("content_sync") or {}).get("state") or "")
        if not content_sync_is_current(segment):
            raise LongFormError(
                f"第 {index} 段内容状态为 {sync_state or 'unknown'}，或内容哈希已变化；请先同步或重生成后续段。",
                "context_content_sync_required",
            )
        workspace = segment.get("single_workspace") or {}
        validation = workspace.get("validation") or {}
        prompt = str(segment.get("h3_prompt") or "")
        if not (
            prompt
            and segment.get("prompt_state") == "valid"
            and workspace.get("state") == "valid"
            and validation.get("valid") is True
        ):
            raise LongFormError(
                f"第 {index} 段没有已保存且通过校验的 H3 完整提示词。",
                "context_prompt_not_ready",
            )
        if str(workspace.get("prompt") or "") != prompt:
            raise LongFormError(
                f"第 {index} 段的已保存工作区提示词与项目 H3 提示词不一致；请重新保存或重新编译本段。",
                "context_prompt_source_mismatch",
            )
        if segment.get("script_state") != "ready" or segment.get("timeline_state") != "valid":
            raise LongFormError(
                f"第 {index} 段 Shot 或时间线尚未就绪。",
                "context_script_not_ready",
            )
        form = workspace.get("form") or {}
        mode = str(form.get("mode") or "")
        boundary = str(segment.get("boundary_before") or "")
        if mode not in {"T2VA", "I2VA", "FL2VA"}:
            raise LongFormError(
                f"第 {index} 段模式无效。", "context_mode_invalid"
            )
        first_frame: dict[str, str] | None = None
        last_frame: dict[str, str] | None = None
        continuous = index > 1 and boundary == "continuous"
        if continuous:
            if mode not in {"I2VA", "FL2VA"}:
                raise LongFormError(
                    f"第 {index} 段是连续边界，必须使用 I2VA 或 FL2VA。",
                    "context_mode_invalid",
                )
            first_frame = {"source": "previous_tail", "name": ""}
        elif mode in {"I2VA", "FL2VA"}:
            first_frame = _saved_input_picture(
                segment, "picture1", required=True
            )
        if mode == "FL2VA":
            last_frame = _saved_input_picture(segment, "picture2", required=True)
        elif _saved_input_picture(segment, "picture2", required=False):
            warnings.append(f"第 {index} 段不是 FL2VA，已忽略已保存的 Picture 2。")

        target_frames = int(segment.get("frames") or 0)
        raw_frames = h3_model_frames(target_frames, continuous=continuous)
        scene_id = str(segment.get("id") or f"seg_{index:04d}")
        scenes.append(
            {
                "id": scene_id,
                "source_segment_id": scene_id,
                "source_index": index,
                "source_hash": segment_source_hash(segment),
                "prompt": prompt,
                "mode": mode,
                "boundary_before": boundary,
                "first_frame": first_frame,
                "last_frame": last_frame,
                "target_frames": target_frames,
                "delivered_frames": target_frames,
                "raw_frames": raw_frames,
                "actual_seconds": target_frames / FPS,
                "steps": LOCKED_SETTINGS["steps"],
                "seed": str(derive_scene_seed(seed, index, scene_id)),
                "continuity_summary": str(
                    (segment.get("story_card") or {}).get("ending_state") or ""
                ),
                "warnings": [],
                "provenance": "project_h3_prompt",
                "manual_revision": int(segment.get("manual_revision") or 0),
            }
        )

    now = utc_now()
    use_upscale = (
        bool((prior.get("outputs") or {}).get("upscale_1080"))
        if upscale_1080 is None
        else bool(upscale_1080)
    )
    spec = {
        "schema": CONTEXT_SCHEMA,
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "project_id": project["id"],
        "source_revision": int(project.get("current_revision") or 0),
        "source_fingerprint": project_source_fingerprint(project),
        "run_name": str(prior.get("run_name") or _safe_run_name(project["id"])),
        "revision": int(prior.get("revision") or 0),
        "status": "valid",
        "stale": False,
        "stale_from": None,
        "base_seed": str(seed),
        "settings": copy.deepcopy(LOCKED_SETTINGS),
        "outputs": {"native": True, "upscale_1080": use_upscale},
        "references": {"opening_image": None, "identity_images": []},
        "scenes": scenes,
        "total_delivered_frames": sum(int(item["target_frames"]) for item in scenes),
        "actual_seconds": sum(int(item["target_frames"]) for item in scenes) / FPS,
        "warnings": warnings,
        "usage": {},
        "render": {
            "state": "stale" if prior.get("render") else "not_started",
            "prompt_id": "",
            "start_scene": int((prior.get("render") or {}).get("start_scene") or 1),
            "native_path": str((prior.get("render") or {}).get("native_path") or ""),
            "upscaled_path": "",
            "last_error": None,
        },
        "created_at": str(prior.get("created_at") or now),
        "updated_at": now,
    }
    return spec


def make_empty_spec(
    project: dict[str, Any],
    *,
    references: dict[str, Any] | None = None,
    base_seed: int | None = None,
) -> dict[str, Any]:
    segments = project.get("segments") or []
    if not segments:
        raise LongFormError("长视频项目没有分段。", "context_project_invalid")
    if len(segments) > MAX_SHOTS:
        raise LongFormError(
            f"Context Loop 最多支持 {MAX_SHOTS} 段。", "context_scene_count_invalid"
        )
    refs = copy.deepcopy(references or reference_spec(project))
    seed = int(base_seed if base_seed is not None else secrets.randbits(64))
    if seed < 0 or seed > MAX_SEED:
        raise LongFormError("基础 Seed 超出范围。", "context_seed_invalid")
    now = utc_now()
    return {
        "schema": CONTEXT_SCHEMA,
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "project_id": project["id"],
        "source_revision": int(project.get("current_revision") or 0),
        "source_fingerprint": project_source_fingerprint(project),
        "run_name": _safe_run_name(project["id"]),
        "revision": 0,
        "status": "draft",
        "stale": False,
        "stale_from": None,
        "base_seed": str(seed),
        "settings": copy.deepcopy(LOCKED_SETTINGS),
        "outputs": {"native": True, "upscale_1080": False},
        "references": refs,
        "scenes": [],
        "total_delivered_frames": 0,
        "actual_seconds": 0.0,
        "warnings": [],
        "usage": {},
        "render": {
            "state": "not_started",
            "prompt_id": "",
            "start_scene": 1,
            "native_path": "",
            "upscaled_path": "",
            "last_error": None,
        },
        "created_at": now,
        "updated_at": now,
    }


def finalize_spec(spec: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    scenes = spec.get("scenes") or []
    source_segments = project.get("segments") or []
    if len(scenes) != len(source_segments):
        raise LongFormError("高级计划段数与剧本不一致。", "context_scene_count_mismatch")
    identity_count = len((spec.get("references") or {}).get("identity_images") or [])
    has_opening = bool((spec.get("references") or {}).get("opening_image"))
    total = 0
    warnings: list[str] = []
    for index, (scene, segment) in enumerate(zip(scenes, source_segments), start=1):
        expected_id = str(segment.get("id") or "")
        if str(scene.get("id") or "") != expected_id:
            raise LongFormError("高级计划场景顺序或 ID 已改变。", "context_scene_id_mismatch")
        timing = calibrate_scene_duration(
            scene.get("suggested_duration_seconds"), scene_index=index
        )
        scene.update(timing)
        scene["source_segment_id"] = expected_id
        scene["source_index"] = index
        scene["source_hash"] = segment_source_hash(segment)
        scene["steps"] = LOCKED_SETTINGS["steps"]
        try:
            seed = int(str(scene.get("seed") or ""))
        except ValueError as exc:
            raise LongFormError("场景 Seed 必须是整数。", "context_seed_invalid") from exc
        if seed < 0 or seed > MAX_SEED:
            raise LongFormError("场景 Seed 超出范围。", "context_seed_invalid")
        scene["seed"] = str(seed)
        errors = validate_scene_prompt(
            str(scene.get("prompt") or ""),
            segment=segment,
            scene_index=index,
            actual_seconds=timing["actual_seconds"],
            identity_count=identity_count,
            has_opening_image=has_opening,
        )
        if errors:
            raise LongFormError(
                f"第 {index} 段：" + "；".join(errors), "context_prompt_invalid"
            )
        total += int(timing["delivered_frames"])
        warnings.extend(str(item) for item in scene.get("warnings") or [] if str(item))
    spec["settings"] = copy.deepcopy(LOCKED_SETTINGS)
    spec["outputs"] = {
        "native": True,
        "upscale_1080": bool((spec.get("outputs") or {}).get("upscale_1080")),
    }
    spec["total_delivered_frames"] = total
    spec["actual_seconds"] = total / FPS
    spec["warnings"] = list(dict.fromkeys(warnings))
    spec["source_revision"] = int(project.get("current_revision") or 0)
    spec["source_fingerprint"] = project_source_fingerprint(project)
    spec["status"] = "valid"
    spec["stale"] = False
    spec["stale_from"] = None
    return spec


def generation_fingerprint(spec: dict[str, Any]) -> str:
    """Hash non-plan inputs only; prompts are protected by plugin history hashes."""

    return _hash(
        {
            "schema": CONTEXT_SCHEMA,
            "settings": spec.get("settings") or {},
            "keyframes": [
                {
                    "id": item.get("id"),
                    "mode": item.get("mode"),
                    "boundary_before": item.get("boundary_before"),
                    "first_frame": item.get("first_frame"),
                    "last_frame": item.get("last_frame"),
                }
                for item in spec.get("scenes") or []
            ],
        }
    )


def assess_staleness(spec: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(spec)
    first: int | None = (
        None
        if value.get("schema_version") == CONTEXT_SCHEMA_VERSION
        else 1
    )
    source_segments = project.get("segments") or []
    scenes = value.get("scenes") or []
    if first is None and len(scenes) != len(source_segments):
        first = min(len(scenes), len(source_segments)) + 1
    elif first is None:
        for index, (scene, segment) in enumerate(zip(scenes, source_segments), start=1):
            if (
                str(scene.get("id") or "") != str(segment.get("id") or "")
                or str(scene.get("source_hash") or "") != segment_source_hash(segment)
            ):
                first = index
                break
    if value.get("source_fingerprint") != project_source_fingerprint(project) and first is None:
        first = 1
    value["stale"] = first is not None
    value["stale_from"] = first
    if first is not None and value.get("status") not in {"running", "failed"}:
        value["status"] = "stale"
    return value


def normalize_edited_spec(
    raw: Any,
    *,
    current: dict[str, Any],
    project: dict[str, Any],
) -> tuple[dict[str, Any], int | None]:
    if not isinstance(raw, dict):
        raise LongFormError("高级 JSON 顶层必须是对象。", "context_spec_invalid")
    if current.get("source_fingerprint") != project_source_fingerprint(project):
        raise LongFormError(
            "源剧本已经变化，请先重建高级计划。", "context_source_stale"
        )
    for name, expected in (
        ("schema", CONTEXT_SCHEMA),
        ("schema_version", CONTEXT_SCHEMA_VERSION),
        ("project_id", project["id"]),
        ("run_name", current["run_name"]),
    ):
        if raw.get(name) != expected:
            raise LongFormError(f"禁止修改 {name}。", "context_locked_field")
    if raw.get("settings") != current.get("settings"):
        raise LongFormError("模型和 Context 设置为锁定字段。", "context_locked_field")
    if raw.get("references") != current.get("references"):
        raise LongFormError("请通过参考图选择器修改图片。", "context_locked_field")
    incoming_scenes = raw.get("scenes")
    if not isinstance(incoming_scenes, list) or len(incoming_scenes) != len(current["scenes"]):
        raise LongFormError("禁止通过高级 JSON 增删场景。", "context_scene_count_mismatch")
    editable = copy.deepcopy(current)
    changed_from: int | None = None
    for index, (target, incoming) in enumerate(
        zip(editable["scenes"], incoming_scenes), start=1
    ):
        if not isinstance(incoming, dict) or incoming.get("id") != target.get("id"):
            raise LongFormError("禁止修改场景 ID 或顺序。", "context_scene_id_mismatch")
        before = (
            target.get("prompt"),
            target.get("suggested_duration_seconds"),
            str(target.get("seed")),
        )
        target["prompt"] = _clean_text(incoming.get("prompt"))
        target["suggested_duration_seconds"] = incoming.get(
            "suggested_duration_seconds"
        )
        target["seed"] = str(incoming.get("seed") or "")
        after = (
            target.get("prompt"),
            target.get("suggested_duration_seconds"),
            str(target.get("seed")),
        )
        if before != after:
            target["provenance"] = "manual"
            target["manual_revision"] = int(target.get("manual_revision") or 0) + 1
            if changed_from is None:
                changed_from = index
    editable["outputs"] = {
        "native": True,
        "upscale_1080": bool((raw.get("outputs") or {}).get("upscale_1080")),
    }
    finalized = finalize_spec(editable, project)
    if changed_from is not None:
        finalized["render"] = {
            "state": "stale",
            "prompt_id": "",
            "start_scene": changed_from,
            "native_path": str((current.get("render") or {}).get("native_path") or ""),
            "upscaled_path": "",
            "last_error": None,
        }
    return finalized, changed_from


class ContextLoopStore:
    """Atomic Context Loop sidecars below an existing LongProjectStore."""

    def __init__(self, long_store: LongProjectStore) -> None:
        self.long_store = long_store

    def root(self, project_id: str) -> Path:
        return self.long_store.project_dir(project_id) / "context_loop"

    def path(self, project_id: str) -> Path:
        return self.root(project_id) / "spec.json"

    def load_optional(self, project_id: str) -> dict[str, Any] | None:
        with self.long_store.transaction():
            path = self.path(project_id)
            if not path.is_file():
                return None
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise LongFormError(
                    "Context Loop Sidecar 已损坏。", "context_spec_invalid"
                ) from exc
            if not isinstance(value, dict) or value.get("schema") != CONTEXT_SCHEMA:
                raise LongFormError("Context Loop Sidecar 格式无效。", "context_spec_invalid")
            return value

    def load(self, project_id: str) -> dict[str, Any]:
        value = self.load_optional(project_id)
        if value is None:
            raise LongFormError("尚未生成规则工作流。", "context_plan_not_found")
        return value

    def save(
        self,
        value: dict[str, Any],
        *,
        expected_revision: int | None = None,
        reason: str = "save",
    ) -> dict[str, Any]:
        project_id = str(value.get("project_id") or "")
        with self.long_store.transaction():
            current = self.load_optional(project_id)
            current_revision = int((current or {}).get("revision") or 0)
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise LongFormError(
                    "规则工作流已在其他页面更新，请刷新后重试。",
                    "context_revision_conflict",
                )
            if current is not None:
                revision_path = self.root(project_id) / "revisions" / f"{current_revision:06d}.json"
                self.long_store._atomic_json(revision_path, current)
            saved = copy.deepcopy(value)
            saved["revision"] = current_revision + 1
            saved["updated_at"] = utc_now()
            saved.setdefault("created_at", saved["updated_at"])
            saved["last_save_reason"] = str(reason)[:300]
            self.long_store._atomic_json(self.path(project_id), saved)
            return saved

    def public(self, project: dict[str, Any]) -> dict[str, Any]:
        value = self.load_optional(project["id"])
        if value is None:
            return {
                "exists": False,
                "schema": CONTEXT_SCHEMA,
                "project_id": project["id"],
                "status": "not_created",
                "stale": False,
                "stale_from": None,
            }
        assessed = assess_staleness(value, project)
        assessed["exists"] = True
        return assessed


def merge_spec_usage(spec: dict[str, Any], usage: dict[str, Any]) -> None:
    target = spec.setdefault("usage", {})
    merge_usage(target, usage)
