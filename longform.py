"""Long-form story planning and durable project state for H3 Prompt Studio.

This module is deliberately provider- and ComfyUI-agnostic.  It owns the
24-fps frame allocation, long-project schema, revisions, manual edits, and the
anchor-aware regeneration plan.  Network and GPU orchestration live in
``longform_runtime.py`` so all of the rules here can be tested offline.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


FPS = 24
NOMINAL_SEGMENT_FRAMES = 120  # Target about 5 seconds per story card.
MIN_RELIABLE_OUTPUT_FRAMES = 120  # 5.00 seconds at 24 fps
MAX_RELIABLE_OUTPUT_FRAMES = 360  # 15.00 seconds at 24 fps
MIN_H3_MODEL_FRAMES = 124
H3_FRAME_MODULUS = 17
H3_FRAME_REMAINDER = 5
SCHEMA_VERSION = 6
MAX_SEGMENT_BEATS = 64
TIMELINE_STATES = {"valid", "needs_review", "invalid"}
LEGACY_TIME_RE = re.compile(
    r"(?:\b\d{1,2}:\d{2}(?:\.\d+)?\b|(?<![\w.])\d+(?:\.\d+)?\s*(?:s|sec(?:onds?)?|秒))",
    re.IGNORECASE,
)

PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
SEGMENT_ID_RE = re.compile(r"^seg_(\d{4,})$")
BOUNDARY_TYPES = {"start", "continuous", "cut"}
PROJECT_STATES = {
    "draft",
    "ready",
    "stale",
    "waiting_comfy",
    "running",
    "retrying",
    "paused",
    "failed",
    "completed",
}
CONTENT_SYNC_STATES = {"clean", "story_dirty", "shots_dirty", "failed"}
AUTHORING_CONFIRMATION_STATES = {"unconfirmed", "confirmed"}


class LongFormError(ValueError):
    """Expected validation or state-transition failure."""

    def __init__(self, message: str, code: str = "long_project_invalid") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def empty_authoring_confirmation() -> dict[str, Any]:
    return {
        "state": "unconfirmed",
        "fingerprint": "",
        "confirmed_at": "",
        "provider": "",
        "model": "",
    }


def _authoring_payload(project: dict[str, Any]) -> dict[str, Any]:
    """Return only story/prompt content that must be frozen before GPU render."""

    top_level = {
        key: copy.deepcopy(project.get(key))
        for key in (
            "title",
            "idea",
            "language",
            "aspect_ratio",
            "fps",
            "target_frames",
            "actual_seconds",
            "story_bible",
            "outline",
            "ending_requirements",
            "reference_analysis",
            "identity_references",
        )
    }
    segments: list[dict[str, Any]] = []
    for segment in project.get("segments") or []:
        workspace = segment.get("single_workspace") or {}
        segments.append(
            {
                key: copy.deepcopy(segment.get(key))
                for key in (
                    "id",
                    "index",
                    "frames",
                    "boundary_before",
                    "story_target",
                    "story_card",
                    "content_sync",
                    "beats",
                    "visual",
                    "camera",
                    "dialogue",
                    "visible_text",
                    "sound",
                    "music",
                    "continuity_in",
                    "continuity_out",
                    "extra_constraints",
                    "script_state",
                    "timeline_state",
                    "prompt_state",
                    "h3_prompt",
                    "covered_outline_chapters",
                    "fulfilled_ending_requirements",
                )
            }
        )
        segments[-1]["single_workspace"] = {
            key: copy.deepcopy(workspace.get(key))
            for key in (
                "form",
                "script",
                "pictures",
                "prompt",
                "validation",
                "warnings",
                "state",
            )
        }
    top_level["segments"] = segments
    return top_level


def authoring_fingerprint(project: dict[str, Any]) -> str:
    encoded = json.dumps(
        _authoring_payload(project),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def authoring_confirmation_is_current(project: dict[str, Any]) -> bool:
    confirmation = project.get("authoring_confirmation") or {}
    fingerprint = str(confirmation.get("fingerprint") or "")
    return (
        confirmation.get("state") == "confirmed"
        and bool(re.fullmatch(r"[0-9a-f]{64}", fingerprint))
        and fingerprint == authoring_fingerprint(project)
    )


def confirm_authoring(
    project: dict[str, Any], *, provider: str, model: str
) -> dict[str, Any]:
    project["authoring_confirmation"] = {
        "state": "confirmed",
        "fingerprint": authoring_fingerprint(project),
        "confirmed_at": utc_now(),
        "provider": str(provider),
        "model": str(model),
    }
    return project["authoring_confirmation"]


def _round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


@dataclass(frozen=True)
class FramePlan:
    total_frames: int
    segment_frames: tuple[int, ...]

    @property
    def segment_count(self) -> int:
        return len(self.segment_frames)

    @property
    def actual_seconds(self) -> float:
        return self.total_frames / FPS

    def as_dict(self) -> dict[str, Any]:
        return {
            "fps": FPS,
            "total_frames": self.total_frames,
            "actual_seconds": self.actual_seconds,
            "segment_count": self.segment_count,
            "segment_frames": list(self.segment_frames),
            "segment_seconds": [frames / FPS for frames in self.segment_frames],
        }


def allocate_segment_frames(
    target_seconds: float,
    *,
    segment_count: int | None = None,
) -> FramePlan:
    """Convert a requested duration to exact 24-fps frames and distribute them.

    With no explicit count, choose the nearest count around five seconds while
    never creating an under-five-second production segment.  Across a fixed
    plan, segment lengths differ by at most one frame.
    """

    try:
        seconds = float(target_seconds)
    except (TypeError, ValueError) as exc:
        raise LongFormError("目标时长必须是数字。", "target_duration_invalid") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise LongFormError("目标时长必须大于 0 秒。", "target_duration_invalid")
    total_frames = max(1, _round_half_up(seconds * FPS))

    if segment_count is None:
        count = max(1, _round_half_up(total_frames / NOMINAL_SEGMENT_FRAMES))
        while count > 1 and total_frames // count < MIN_RELIABLE_OUTPUT_FRAMES:
            count -= 1
    else:
        try:
            count = int(segment_count)
        except (TypeError, ValueError) as exc:
            raise LongFormError("段数必须是正整数。", "segment_count_invalid") from exc
        if count <= 0:
            raise LongFormError("段数必须是正整数。", "segment_count_invalid")
        if total_frames < count:
            raise LongFormError("总帧数不能少于段数。", "segment_count_invalid")
        shortest = total_frames // count
        longest = math.ceil(total_frames / count)
        if count > 1 and shortest < MIN_RELIABLE_OUTPUT_FRAMES:
            raise LongFormError(
                "重新规划后每段必须至少约 5 秒；请减少段数。",
                "segment_count_invalid",
            )
        if longest > MAX_RELIABLE_OUTPUT_FRAMES:
            raise LongFormError(
                "重新规划后每段不能超过 15 秒；请增加段数。",
                "segment_count_invalid",
            )

    base, extra = divmod(total_frames, count)
    frames = tuple(base + (1 if index < extra else 0) for index in range(count))
    if max(frames) - min(frames) > 1:  # pragma: no cover - divmod invariant
        raise AssertionError("frame allocation drift")
    return FramePlan(total_frames=total_frames, segment_frames=frames)


def align_h3_frame_count(value: int) -> int:
    result = max(MIN_H3_MODEL_FRAMES, int(value))
    while result % H3_FRAME_MODULUS != H3_FRAME_REMAINDER:
        result += 1
    return result


def h3_model_frames(output_frames: int, *, continuous: bool) -> int:
    required = int(output_frames) + (1 if continuous else 0)
    return align_h3_frame_count(required)


def uniform_frame_indices(
    source_frames: int,
    output_frames: int,
    *,
    skip_first: bool,
) -> list[int]:
    """Select exact endpoints with nearest-position integer mapping."""

    start = 1 if skip_first else 0
    end = int(source_frames) - 1
    count = int(output_frames)
    available = end - start + 1
    if count <= 0 or available < count:
        raise LongFormError("模型帧不足以生成目标输出帧。", "frame_mapping_invalid")
    if count == 1:
        return [end]
    span = end - start
    denominator = count - 1
    values = [
        start + (index * span + denominator // 2) // denominator
        for index in range(count)
    ]
    if values[0] != start or values[-1] != end or len(set(values)) != count:
        raise LongFormError("帧重映射没有完整保留首尾。", "frame_mapping_invalid")
    return values


def format_seconds(frames: int) -> str:
    return f"{int(frames) / FPS:.2f}"


def build_segment_story_targets(
    outline: list[dict[str, Any]],
    ending_requirements: list[str],
    segment_count: int,
) -> list[dict[str, Any]]:
    """Map every outline chapter onto exact segments without another model call."""

    if not isinstance(outline, list) or not outline or int(segment_count) <= 0:
        raise LongFormError("无法建立分段剧情目标。", "segment_story_target_invalid")
    chapter_count = len(outline)
    count = int(segment_count)
    assignments: list[list[int]] = []
    for index in range(count):
        # Integer half-up boundaries avoid Python's bankers rounding.
        start = (2 * index * chapter_count + count) // (2 * count)
        end = (2 * (index + 1) * chapter_count + count) // (2 * count)
        if end > start:
            assigned = list(range(start, min(end, chapter_count)))
        else:
            midpoint = min(
                chapter_count - 1,
                ((2 * index + 1) * chapter_count) // (2 * count),
            )
            assigned = [midpoint]
        assignments.append(assigned)
    assignments[-1] = sorted(set(assignments[-1] + [chapter_count - 1]))

    occurrence_counts: dict[int, int] = {}
    for assigned in assignments:
        for chapter_index in assigned:
            occurrence_counts[chapter_index] = occurrence_counts.get(chapter_index, 0) + 1
    occurrence_seen: dict[int, int] = {}
    targets: list[dict[str, Any]] = []
    for index, assigned in enumerate(assignments):
        phases = []
        chapter_numbers = []
        chapters = []
        for chapter_index in assigned:
            occurrence_seen[chapter_index] = occurrence_seen.get(chapter_index, 0) + 1
            raw_chapter = outline[chapter_index]
            try:
                chapter_number = int(raw_chapter.get("chapter") or chapter_index + 1)
            except (TypeError, ValueError):
                chapter_number = chapter_index + 1
            chapter_numbers.append(chapter_number)
            chapters.append(copy.deepcopy(raw_chapter))
            phases.append(
                {
                    "chapter": chapter_number,
                    "occurrence": occurrence_seen[chapter_index],
                    "occurrences": occurrence_counts[chapter_index],
                }
            )
        is_final = index == count - 1
        targets.append(
            {
                "segment_index": index + 1,
                "segment_count": count,
                "story_progress_start": round(index / count, 6),
                "story_progress_end": round((index + 1) / count, 6),
                "chapter_numbers": chapter_numbers,
                "outline_chapters": chapters,
                "chapter_phases": phases,
                "required_ending_conditions": (
                    list(ending_requirements) if is_final else []
                ),
                "must_close_story": is_final,
            }
        )
    covered = {number for target in targets for number in target["chapter_numbers"]}
    expected = {
        int(item.get("chapter") or index + 1)
        if str(item.get("chapter") or index + 1).lstrip("-").isdigit()
        else index + 1
        for index, item in enumerate(outline)
    }
    if not expected.issubset(covered):  # pragma: no cover - integer mapping invariant
        raise AssertionError("outline chapter mapping is incomplete")
    return targets


def canonical_beat_seconds(frame: int) -> str:
    value = (Decimal(int(frame)) / Decimal(FPS)).quantize(
        Decimal("0.001"), rounding=ROUND_HALF_UP
    )
    return f"{value:.3f}"


def _seconds_to_frame(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or value is None:
        raise LongFormError(
            f"Beat 的 {field} 必须是秒数。", "segment_beats_invalid"
        )
    try:
        seconds = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise LongFormError(
            f"Beat 的 {field} 必须是有效秒数。", "segment_beats_invalid"
        ) from exc
    if not seconds.is_finite() or seconds < 0:
        raise LongFormError(
            f"Beat 的 {field} 不能为负数或非有限值。",
            "segment_timeline_out_of_bounds",
        )
    return int((seconds * Decimal(FPS)).to_integral_value(rounding=ROUND_HALF_UP))


def normalize_beats(value: Any, *, frames: int) -> list[dict[str, Any]]:
    """Normalize human-readable seconds onto an exact 24-fps half-open grid."""

    if not isinstance(value, list) or not value:
        raise LongFormError(
            "分段 beats 必须是非空数组。", "segment_beats_invalid"
        )
    if len(value) > MAX_SEGMENT_BEATS:
        raise LongFormError(
            f"单段 beats 不能超过 {MAX_SEGMENT_BEATS} 项。",
            "segment_beats_invalid",
        )
    normalized: list[dict[str, Any]] = []
    previous_end = 0
    for position, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise LongFormError(
                f"Beat {position} 必须是对象。", "segment_beats_invalid"
            )
        action_value = item.get("action")
        if not isinstance(action_value, str):
            raise LongFormError(
                f"Beat {position} 的 action 必须是字符串，不能是数组或对象。",
                "segment_beats_invalid",
            )
        action = _clean_text(action_value, limit=10_000)
        if not action:
            raise LongFormError(
                f"Beat {position} 的 action 不能为空。", "segment_beats_invalid"
            )
        start_frame = _seconds_to_frame(
            item.get("start_seconds"), field=f"Beat {position} start_seconds"
        )
        end_frame = _seconds_to_frame(
            item.get("end_seconds"), field=f"Beat {position} end_seconds"
        )
        if start_frame != previous_end:
            relation = "重叠" if start_frame < previous_end else "空洞"
            raise LongFormError(
                f"Beat {position} 与上一项存在时间{relation}：应从第 {previous_end} 帧开始。",
                "segment_timeline_overlap" if relation == "重叠" else "segment_timeline_gap",
            )
        if end_frame <= start_frame:
            raise LongFormError(
                f"Beat {position} 吸附到帧后没有正时长。",
                "segment_beats_invalid",
            )
        if start_frame < 0 or end_frame > int(frames):
            raise LongFormError(
                f"Beat {position} 超出本段 0–{int(frames)} 帧范围。",
                "segment_timeline_out_of_bounds",
            )
        normalized.append(
            {
                "start_seconds": canonical_beat_seconds(start_frame),
                "end_seconds": canonical_beat_seconds(end_frame),
                "start_frame": start_frame,
                "end_frame": end_frame,
                "action": action,
            }
        )
        previous_end = end_frame
    if previous_end != int(frames):
        raise LongFormError(
            f"最后一个 Beat 必须结束于第 {int(frames)} 帧（{canonical_beat_seconds(frames)} 秒）。",
            "segment_timeline_end_mismatch",
        )
    return normalized


def _clean_text(value: Any, *, limit: int = 100_000) -> str:
    text = str(value or "").replace("\x00", "").strip()
    return text[:limit]


def _clean_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    return [_clean_text(item, limit=20_000) for item in value if _clean_text(item)]


def normalize_dialogue(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LongFormError("对白必须是数组。", "long_dialogue_invalid")
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise LongFormError("对白条目必须是对象。", "long_dialogue_invalid")
        text = _clean_text(item.get("text"), limit=20_000)
        if not text:
            continue
        result.append(
            {
                "speaker": _clean_text(item.get("speaker"), limit=200),
                "language": _clean_text(item.get("language"), limit=100) or "Chinese",
                "text": text,
            }
        )
    return result


def _content_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def story_source_hash(segment: dict[str, Any]) -> str:
    """Hash only the user-authoritative upper story inputs.

    Title, ending state and character presence are deliberately excluded: they
    are derived by the explicit reconciliation call. Opening state is included
    because it is the immutable hand-off supplied by the preceding real tail.
    """

    card = segment.get("story_card") or {}
    return _content_hash(
        {
            "story_text": _clean_text(card.get("story_text"), limit=40_000),
            "opening_state": _clean_text(card.get("opening_state"), limit=20_000),
            "boundary_before": str(
                card.get("boundary_before") or segment.get("boundary_before") or ""
            ),
        }
    )


def shot_source_hash(segment: dict[str, Any]) -> str:
    workspace = segment.get("single_workspace") or {}
    script = workspace.get("script")
    return _content_hash(script if isinstance(script, dict) else None)


def aggregate_shot_dialogue(segment: dict[str, Any]) -> list[dict[str, str]]:
    """Return Shot dialogue in timeline order without asking an LLM to edit it."""

    workspace = segment.get("single_workspace") or {}
    script = workspace.get("script") or {}
    shots = script.get("shots") if isinstance(script, dict) else None
    if not isinstance(shots, list):
        return normalize_dialogue((segment.get("story_card") or {}).get("dialogue"))
    result: list[dict[str, str]] = []
    for shot in shots:
        if isinstance(shot, dict):
            result.extend(normalize_dialogue(shot.get("dialogue")))
    return result


def make_content_sync(
    segment: dict[str, Any],
    *,
    state: str = "clean",
    source: str = "generated",
    error: str = "",
) -> dict[str, Any]:
    if state not in CONTENT_SYNC_STATES:
        raise LongFormError("剧情内容同步状态无效。", "content_sync_invalid")
    return {
        "state": state,
        "source": str(source or "generated")[:100],
        "story_hash": story_source_hash(segment),
        "shots_hash": shot_source_hash(segment),
        "synced_at": utc_now() if state == "clean" else "",
        "last_error": _clean_text(error, limit=2_000),
    }


def mark_content_dirty(
    segment: dict[str, Any],
    state: str,
    *,
    source: str,
) -> None:
    segment["content_sync"] = make_content_sync(
        segment,
        state=state,
        source=source,
    )


def content_sync_is_current(segment: dict[str, Any]) -> bool:
    """Return true only when the recorded clean state matches both source layers."""

    sync = segment.get("content_sync") or {}
    return (
        sync.get("state") == "clean"
        and str(sync.get("story_hash") or "") == story_source_hash(segment)
        and str(sync.get("shots_hash") or "") == shot_source_hash(segment)
    )


def make_single_workspace(
    *,
    index: int,
    frames: int,
    boundary_before: str,
    story_card: dict[str, Any] | None = None,
    creative_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the durable state consumed by the existing single-shot studio."""

    card = story_card or {}
    options = creative_options or {}
    dialogue = normalize_dialogue(card.get("dialogue"))
    is_continuous = int(index) > 1 and boundary_before == "continuous"
    result = {
        "revision": 0,
        "state": "empty",
        "form": {
            "mode": "I2VA" if is_continuous else "T2VA",
            "duration": int(frames) / FPS,
            "aspect_ratio": "9:16",
            "creative_brief": _clean_text(card.get("story_text"), limit=40_000),
            "visual_style": _clean_text(options.get("visual_style"), limit=20_000),
            "subjects": _clean_text(options.get("characters"), limit=40_000),
            "scene_lighting": "",
            "action_timeline": _clean_text(card.get("story_text"), limit=40_000),
            "camera_motion": "",
            "exact_dialogue": "\n".join(item["text"] for item in dialogue),
            "visible_text": "",
            "ambient_sound": "",
            "music": _clean_text(options.get("music"), limit=20_000),
            "extra_constraints": _clean_text(options.get("constraints"), limit=40_000),
            "picture1_description": (
                _clean_text(card.get("opening_state"), limit=40_000)
                if is_continuous
                else ""
            ),
            "picture2_description": "",
        },
        "pictures": {
            "picture1": {
                "source": "auto_tail" if is_continuous else "none",
                "input_path": "",
                "temporary_name": "",
            },
            "picture2": {
                "source": "none",
                "input_path": "",
                "temporary_name": "",
            },
        },
        "script": None,
        "prompt": "",
        "validation": {"valid": False, "errors": [], "warnings": []},
        "warnings": [],
        "usage": {},
        "updated_at": utc_now(),
    }
    return result


def _segment_dialogue(segment: dict[str, Any]) -> list[dict[str, str]]:
    card = segment.get("story_card")
    if isinstance(card, dict) and isinstance(card.get("dialogue"), list):
        return [item for item in card["dialogue"] if isinstance(item, dict)]
    return [item for item in segment.get("dialogue") or [] if isinstance(item, dict)]


def exact_dialogue_lines(value: Any) -> list[str]:
    text = _clean_text(value, limit=100_000).replace("\r\n", "\n").replace("\r", "\n")
    return list(dict.fromkeys(line.strip() for line in text.split("\n") if line.strip()))


def missing_required_dialogue(project: dict[str, Any]) -> list[str]:
    required = [str(item) for item in project.get("exact_dialogue_required") or [] if str(item)]
    present = {
        str(dialogue.get("text") or "")
        for segment in project.get("segments") or []
        for dialogue in _segment_dialogue(segment)
        if isinstance(dialogue, dict)
    }
    return [line for line in required if line not in present]


def duplicated_required_dialogue(project: dict[str, Any]) -> list[str]:
    required = {str(item) for item in project.get("exact_dialogue_required") or [] if str(item)}
    counts: dict[str, int] = {}
    for segment in project.get("segments") or []:
        for dialogue in _segment_dialogue(segment):
            if not isinstance(dialogue, dict):
                continue
            text = str(dialogue.get("text") or "")
            if text in required:
                counts[text] = counts.get(text, 0) + 1
    return [line for line, count in counts.items() if count > 1]


def ensure_required_dialogue(project: dict[str, Any]) -> None:
    missing = missing_required_dialogue(project)
    if missing:
        preview = "；".join(missing[:5])
        if len(missing) > 5:
            preview += f"；另有 {len(missing) - 5} 条"
        raise LongFormError(
            "长剧本没有逐字保留全部指定对白：" + preview,
            "long_dialogue_not_preserved",
        )
    duplicated = duplicated_required_dialogue(project)
    if duplicated:
        raise LongFormError(
            "长剧本重复使用了指定对白：" + "；".join(duplicated[:5]),
            "long_dialogue_duplicated",
        )


def normalize_outline_result(raw: Any, *, idea: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LongFormError("长剧本大纲结果必须是 JSON 对象。", "long_outline_invalid")
    value = raw.get("project") if isinstance(raw.get("project"), dict) else raw
    title = _clean_text(value.get("title"), limit=500) or "未命名长视频"
    story_bible = value.get("story_bible")
    if not isinstance(story_bible, dict):
        raise LongFormError("大纲缺少 story_bible。", "long_outline_invalid")
    outline = value.get("outline")
    if not isinstance(outline, list) or not outline:
        raise LongFormError("大纲缺少 outline。", "long_outline_invalid")
    normalized_outline: list[dict[str, Any]] = []
    for index, item in enumerate(outline, start=1):
        if not isinstance(item, dict):
            raise LongFormError("outline 条目必须是对象。", "long_outline_invalid")
        chapter = copy.deepcopy(item)
        try:
            chapter["chapter"] = int(chapter.get("chapter") or index)
        except (TypeError, ValueError) as exc:
            raise LongFormError("outline chapter 必须是整数。", "long_outline_invalid") from exc
        normalized_outline.append(chapter)
    try:
        suggested = float(value.get("suggested_total_seconds") or 0)
    except (TypeError, ValueError) as exc:
        raise LongFormError("建议总时长无效。", "long_outline_invalid") from exc
    reference_analysis = value.get("reference_analysis") or []
    if not isinstance(reference_analysis, list):
        raise LongFormError("reference_analysis 必须是数组。", "long_outline_invalid")
    normalized_references: list[dict[str, Any]] = []
    for item in reference_analysis:
        if not isinstance(item, dict):
            raise LongFormError("reference_analysis 条目必须是对象。", "long_outline_invalid")
        reference_id = _clean_text(item.get("id"), limit=200)
        description = _clean_text(item.get("description"), limit=40_000)
        if not reference_id or not description:
            raise LongFormError("reference_analysis 缺少 id 或 description。", "long_outline_invalid")
        normalized_references.append(
            {
                "id": reference_id,
                "source_name": _clean_text(item.get("source_name"), limit=2_000),
                "description": description,
                "visible_text": _clean_text_list(item.get("visible_text")),
            }
        )
    result = {
        "title": title,
        "language": _clean_text(value.get("language"), limit=100) or "Chinese",
        "idea": _clean_text(idea),
        "suggested_total_seconds": suggested,
        "story_bible": copy.deepcopy(story_bible),
        "outline": normalized_outline,
        "ending_requirements": _clean_text_list(value.get("ending_requirements")),
        "reference_analysis": normalized_references,
        "warnings": _clean_text_list(raw.get("warnings")),
    }
    return result


def normalize_story_card_result(
    raw: Any,
    *,
    index: int,
    story_target: dict[str, Any],
    previous_state: str,
) -> dict[str, Any]:
    """Validate the light-weight story layer before the single-shot stage."""

    if not isinstance(raw, dict):
        raise LongFormError("剧情卡片结果必须是 JSON 对象。", "story_card_invalid")
    value = raw.get("segment") if isinstance(raw.get("segment"), dict) else raw
    # Accept detailed v2 segment JSON during migration/tests, but immediately
    # collapse it into the lightweight story layer.  New prompts only request
    # the fields below and never ask Qwen for render-facing camera/beats here.
    legacy_detailed = "story_text" not in value and "summary" in value
    boundary = _clean_text(value.get("boundary_before"), limit=30).lower()
    if index == 1:
        boundary = "start"
    elif boundary not in {"continuous", "cut"}:
        raise LongFormError("剧情卡片边界必须是 continuous 或 cut。", "story_card_invalid")
    story_text = _clean_text(
        value.get("summary") if legacy_detailed else value.get("story_text"),
        limit=40_000,
    )
    opening_state = _clean_text(
        value.get("continuity_in") if legacy_detailed else value.get("opening_state"),
        limit=20_000,
    )
    ending_state = _clean_text(
        value.get("continuity_out") if legacy_detailed else value.get("ending_state"),
        limit=20_000,
    )
    if not story_text or not ending_state:
        raise LongFormError(
            "剧情卡片必须包含 story_text 和 ending_state。",
            "story_card_invalid",
        )
    covered_raw = value.get("covered_outline_chapters") or []
    if not isinstance(covered_raw, list):
        raise LongFormError("covered_outline_chapters 必须是数组。", "story_card_invalid")
    try:
        covered = [int(item) for item in covered_raw]
    except (TypeError, ValueError) as exc:
        raise LongFormError("剧情卡片章节编号无效。", "story_card_invalid") from exc
    fulfilled = _clean_text_list(value.get("fulfilled_ending_requirements"))
    expected_covered = [int(item) for item in story_target.get("chapter_numbers") or []]
    expected_fulfilled = [
        str(item) for item in story_target.get("required_ending_conditions") or []
    ]
    if covered != expected_covered:
        raise LongFormError(
            "剧情卡片没有覆盖权威章节。", "segment_story_target_mismatch"
        )
    if fulfilled != expected_fulfilled:
        raise LongFormError(
            "剧情卡片没有确认结尾条件。",
            "segment_ending_requirements_mismatch",
        )
    return {
        "title": _clean_text(value.get("title"), limit=500) or f"第 {index} 段",
        "story_text": story_text,
        "dialogue": normalize_dialogue(value.get("dialogue")),
        "opening_state": opening_state
        or _clean_text(previous_state, limit=20_000),
        "ending_state": ending_state,
        "present_characters": _clean_text_list(value.get("present_characters")),
        "boundary_before": boundary,
        "covered_outline_chapters": covered,
        "fulfilled_ending_requirements": fulfilled,
    }


def apply_story_card(
    segment: dict[str, Any],
    card: dict[str, Any],
    *,
    creative_options: dict[str, Any] | None = None,
    provenance: str = "ai",
) -> dict[str, Any]:
    """Attach a story card and reset only the downstream single-shot layer."""

    result = copy.deepcopy(segment)
    result["story_card"] = copy.deepcopy(card)
    result["boundary_before"] = card["boundary_before"]
    result["summary"] = card["story_text"]
    result["visual"] = ""
    result["beats"] = []
    result["covered_outline_chapters"] = list(card["covered_outline_chapters"])
    result["fulfilled_ending_requirements"] = list(
        card["fulfilled_ending_requirements"]
    )
    result["timeline_state"] = "invalid"
    result["camera"] = ""
    result["dialogue"] = copy.deepcopy(card["dialogue"])
    result["visible_text"] = []
    result["sound"] = ""
    result["music"] = ""
    result["present_characters"] = list(card["present_characters"])
    result["continuity_in"] = card["opening_state"]
    result["continuity_out"] = card["ending_state"]
    result["extra_constraints"] = ""
    result["provenance"] = provenance
    result["script_state"] = "planned"
    result["prompt_state"] = "pending"
    result["render_state"] = "pending"
    result["h3_prompt"] = ""
    result["prompt_warnings"] = []
    result["single_workspace"] = make_single_workspace(
        index=int(result["index"]),
        frames=int(result["frames"]),
        boundary_before=result["boundary_before"],
        story_card=card,
        creative_options=creative_options,
    )
    result["content_sync"] = make_content_sync(
        result, state="clean", source="story_generation"
    )
    return result


def normalize_segment_result(
    raw: Any,
    *,
    index: int,
    frames: int,
    previous_state: str,
    story_target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LongFormError("分段结果必须是 JSON 对象。", "long_segment_invalid")
    value = raw.get("segment") if isinstance(raw.get("segment"), dict) else raw
    boundary = _clean_text(value.get("boundary_before"), limit=30).lower()
    if index == 1:
        boundary = "start"
    elif boundary not in {"continuous", "cut"}:
        raise LongFormError("分段边界必须是 continuous 或 cut。", "long_segment_invalid")
    for field in ("summary", "visual", "continuity_out"):
        if not isinstance(value.get(field), str):
            raise LongFormError(
                f"分段字段 {field} 必须是字符串。", "long_segment_invalid"
            )
    summary = _clean_text(value.get("summary"), limit=20_000)
    visual = _clean_text(value.get("visual"), limit=40_000)
    continuity_out = _clean_text(value.get("continuity_out"), limit=20_000)
    beats = normalize_beats(value.get("beats"), frames=int(frames))
    covered_raw = value.get("covered_outline_chapters", [])
    if not isinstance(covered_raw, list):
        raise LongFormError(
            "covered_outline_chapters 必须是数组。", "segment_story_target_invalid"
        )
    try:
        covered_chapters = [int(item) for item in covered_raw]
    except (TypeError, ValueError) as exc:
        raise LongFormError(
            "covered_outline_chapters 必须只包含整数。",
            "segment_story_target_invalid",
        ) from exc
    fulfilled_requirements = _clean_text_list(
        value.get("fulfilled_ending_requirements")
    )
    if story_target is not None:
        expected_chapters = [
            int(item) for item in story_target.get("chapter_numbers") or []
        ]
        expected_requirements = [
            str(item) for item in story_target.get("required_ending_conditions") or []
        ]
        if covered_chapters != expected_chapters:
            raise LongFormError(
                "本段没有逐项确认权威剧情章节：应为 "
                + json.dumps(expected_chapters, ensure_ascii=False),
                "segment_story_target_mismatch",
            )
        if fulfilled_requirements != expected_requirements:
            raise LongFormError(
                "本段没有逐字确认全部结尾条件。",
                "segment_ending_requirements_mismatch",
            )
    if not summary or not visual or not continuity_out:
        raise LongFormError(
            "分段必须包含 summary、visual、beats 和 continuity_out。",
            "long_segment_invalid",
        )
    result = {
        "id": f"seg_{index:04d}",
        "index": index,
        "frames": int(frames),
        "duration_seconds": int(frames) / FPS,
        "duration_display": format_seconds(frames),
        "boundary_before": boundary,
        "summary": summary,
        "visual": visual,
        "beats": beats,
        "covered_outline_chapters": covered_chapters,
        "fulfilled_ending_requirements": fulfilled_requirements,
        "story_target": copy.deepcopy(story_target) if story_target else {},
        "timeline_state": "valid",
        "legacy_action": "",
        "camera": _clean_text(value.get("camera"), limit=20_000),
        "dialogue": normalize_dialogue(value.get("dialogue")),
        "visible_text": _clean_text_list(value.get("visible_text")),
        "sound": _clean_text(value.get("sound"), limit=20_000),
        "music": _clean_text(value.get("music"), limit=20_000),
        "present_characters": _clean_text_list(value.get("present_characters")),
        "continuity_in": _clean_text(value.get("continuity_in"), limit=20_000)
        or _clean_text(previous_state, limit=20_000),
        "continuity_out": continuity_out,
        "extra_constraints": _clean_text(value.get("extra_constraints"), limit=20_000),
        "provenance": "ai",
        "manual_revision": 0,
        "script_state": "ready",
        "prompt_state": "pending",
        "render_state": "pending",
        "locked": False,
        "h3_prompt": "",
        "prompt_warnings": [],
        "script_attempts": [],
        "attempts": [],
        "artifacts": {},
        "qc": {},
    }
    card = {
        "title": f"第 {index} 段",
        "story_text": summary,
        "dialogue": copy.deepcopy(result["dialogue"]),
        "opening_state": result["continuity_in"],
        "ending_state": continuity_out,
        "present_characters": list(result["present_characters"]),
        "boundary_before": boundary,
        "covered_outline_chapters": list(covered_chapters),
        "fulfilled_ending_requirements": list(fulfilled_requirements),
    }
    result["story_card"] = card
    workspace = make_single_workspace(
        index=index,
        frames=frames,
        boundary_before=boundary,
        story_card=card,
    )
    workspace["state"] = "draft"
    result["single_workspace"] = workspace
    result["content_sync"] = make_content_sync(
        result, state="clean", source="segment_generation"
    )
    return result


def make_project(
    outline: dict[str, Any],
    frame_plan: FramePlan,
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    run_id = project_id or (
        datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    )
    if not PROJECT_ID_RE.fullmatch(run_id):
        raise LongFormError("项目 ID 无效。", "project_id_invalid")
    now = utc_now()
    segments = []
    for index, frames in enumerate(frame_plan.segment_frames, start=1):
        segments.append(
            {
                "id": f"seg_{index:04d}",
                "index": index,
                "frames": frames,
                "duration_seconds": frames / FPS,
                "duration_display": format_seconds(frames),
                "boundary_before": "start" if index == 1 else "continuous",
                "summary": "",
                "visual": "",
                "beats": [],
                "covered_outline_chapters": [],
                "fulfilled_ending_requirements": [],
                "timeline_state": "invalid",
                "legacy_action": "",
                "camera": "",
                "dialogue": [],
                "visible_text": [],
                "sound": "",
                "music": "",
                "present_characters": [],
                "continuity_in": "",
                "continuity_out": "",
                "extra_constraints": "",
                "provenance": "placeholder",
                "manual_revision": 0,
                "script_state": "pending",
                "prompt_state": "pending",
                "render_state": "pending",
                "locked": False,
                "h3_prompt": "",
                "prompt_warnings": [],
                "script_attempts": [],
                "attempts": [],
                "artifacts": {},
                "qc": {},
            }
        )
    story_targets = build_segment_story_targets(
        outline["outline"],
        list(outline.get("ending_requirements") or []),
        len(segments),
    )
    for segment, story_target in zip(segments, story_targets):
        segment["story_target"] = story_target
        card = {
            "title": f"第 {segment['index']} 段",
            "story_text": "",
            "dialogue": [],
            "opening_state": "",
            "ending_state": "",
            "present_characters": [],
            "boundary_before": segment["boundary_before"],
            "covered_outline_chapters": list(story_target["chapter_numbers"]),
            "fulfilled_ending_requirements": list(
                story_target["required_ending_conditions"]
            ),
        }
        segment["story_card"] = card
        segment["single_workspace"] = make_single_workspace(
            index=segment["index"],
            frames=segment["frames"],
            boundary_before=segment["boundary_before"],
            story_card=card,
        )
        segment["content_sync"] = make_content_sync(
            segment, state="clean", source="placeholder"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "id": run_id,
        "title": outline["title"],
        "idea": outline["idea"],
        "language": outline["language"],
        "aspect_ratio": "9:16",
        "fps": FPS,
        "target_frames": frame_plan.total_frames,
        "actual_seconds": frame_plan.actual_seconds,
        "status": "draft",
        "story_bible": copy.deepcopy(outline["story_bible"]),
        "outline": copy.deepcopy(outline["outline"]),
        "ending_requirements": list(outline.get("ending_requirements") or []),
        "reference_analysis": copy.deepcopy(outline.get("reference_analysis") or []),
        "warnings": list(outline.get("warnings") or []),
        "outline_attempts": [],
        "script_attempt_log": [],
        "segments": segments,
        "identity_references": {},
        "master": {},
        "current_revision": 0,
        "revision_history": [],
        "stale_from": None,
        "usage": {},
        "authoring_confirmation": empty_authoring_confirmation(),
        "scheduler": {
            "current_segment": None,
            "current_prompt_id": "",
            "stop_after_current": False,
            "last_error": None,
        },
        "created_at": now,
        "updated_at": now,
    }


def _story_card_from_existing(
    segment: dict[str, Any], target: dict[str, Any]
) -> dict[str, Any]:
    existing = segment.get("story_card")
    if isinstance(existing, dict) and existing.get("story_text"):
        card = copy.deepcopy(existing)
    else:
        card = {
            "title": f"第 {int(segment.get('index') or 0)} 段",
            "story_text": _clean_text(segment.get("summary"), limit=40_000),
            "dialogue": normalize_dialogue(segment.get("dialogue")),
            "opening_state": _clean_text(segment.get("continuity_in"), limit=20_000),
            "ending_state": _clean_text(segment.get("continuity_out"), limit=20_000),
            "present_characters": _clean_text_list(segment.get("present_characters")),
            "boundary_before": str(segment.get("boundary_before") or "continuous"),
        }
    card.setdefault("title", f"第 {int(segment.get('index') or 0)} 段")
    card.setdefault("story_text", "")
    card["dialogue"] = normalize_dialogue(card.get("dialogue"))
    card.setdefault("opening_state", _clean_text(segment.get("continuity_in")))
    card.setdefault("ending_state", _clean_text(segment.get("continuity_out")))
    card["present_characters"] = _clean_text_list(card.get("present_characters"))
    card["boundary_before"] = str(segment.get("boundary_before") or "continuous")
    card["covered_outline_chapters"] = [
        int(item) for item in target.get("chapter_numbers") or []
    ]
    card["fulfilled_ending_requirements"] = [
        str(item) for item in target.get("required_ending_conditions") or []
    ]
    return card


def _workspace_from_existing(
    segment: dict[str, Any], card: dict[str, Any]
) -> dict[str, Any]:
    existing = segment.get("single_workspace")
    if isinstance(existing, dict) and isinstance(existing.get("form"), dict):
        workspace = copy.deepcopy(existing)
        workspace.setdefault("pictures", {})
        workspace["pictures"].setdefault(
            "picture1", {"source": "none", "input_path": "", "temporary_name": ""}
        )
        workspace["pictures"].setdefault(
            "picture2", {"source": "none", "input_path": "", "temporary_name": ""}
        )
        workspace.setdefault("validation", {"valid": False, "errors": [], "warnings": []})
        workspace.setdefault("warnings", [])
        workspace.setdefault("usage", {})
        workspace.setdefault("revision", 0)
        workspace.setdefault("prompt", str(segment.get("h3_prompt") or ""))
        workspace.setdefault("state", "valid" if segment.get("prompt_state") == "valid" else "draft")
        return workspace
    workspace = make_single_workspace(
        index=int(segment.get("index") or 1),
        frames=int(segment.get("frames") or 1),
        boundary_before=str(segment.get("boundary_before") or "continuous"),
        story_card=card,
    )
    beats = segment.get("beats") or []
    if beats:
        dialogue = copy.deepcopy(segment.get("dialogue") or [])
        shots = []
        for shot_index, beat in enumerate(beats, start=1):
            shots.append(
                {
                    "shot": shot_index,
                    "start": float(beat["start_frame"]) / FPS,
                    "end": float(beat["end_frame"]) / FPS,
                    "visual": str(segment.get("visual") or ""),
                    "action": str(beat.get("action") or ""),
                    "camera": str(segment.get("camera") or ""),
                    "dialogue": dialogue if shot_index == 1 else [],
                    "visible_text": list(segment.get("visible_text") or []) if shot_index == 1 else [],
                    "sound": str(segment.get("sound") or ""),
                    "music": str(segment.get("music") or ""),
                }
            )
        workspace["script"] = {
            "title": card.get("title") or f"第 {segment.get('index')} 段",
            "logline": str(segment.get("summary") or ""),
            "duration": int(segment.get("frames") or 0) / FPS,
            "aspect_ratio": "9:16",
            "shots": shots,
        }
        workspace["state"] = "draft"
    prompt = str(segment.get("h3_prompt") or "")
    if prompt:
        workspace["prompt"] = prompt
        valid = segment.get("prompt_state") == "valid"
        workspace["validation"] = {"valid": valid, "errors": [], "warnings": []}
        workspace["state"] = "valid" if valid else "stale"
    return workspace


def migrate_project(value: Any) -> tuple[dict[str, Any], bool]:
    """Upgrade schema v1/v2 projects into the story-card + single-workspace model."""

    if not isinstance(value, dict):
        raise LongFormError("项目必须是 JSON 对象。")
    version = int(value.get("schema_version") or 0)
    if version not in {1, 2, 3, 4, 5, SCHEMA_VERSION}:
        raise LongFormError("项目 schema 版本不受支持。", "project_schema_invalid")
    segments = value.get("segments") or []
    complete_current = version == SCHEMA_VERSION and all(
        isinstance(item, dict)
        and isinstance(item.get("story_card"), dict)
        and isinstance(item.get("single_workspace"), dict)
        and isinstance(item["single_workspace"].get("revision"), int)
        and item.get("story_target")
        and isinstance(item.get("content_sync"), dict)
        for item in segments
    ) and isinstance(value.get("authoring_confirmation"), dict)
    if complete_current:
        drifted = copy.deepcopy(value)
        changed = False
        for segment in drifted.get("segments") or []:
            sync = segment.get("content_sync") or {}
            if sync.get("state") != "clean":
                continue
            story_changed = str(sync.get("story_hash") or "") != story_source_hash(segment)
            shots_changed = str(sync.get("shots_hash") or "") != shot_source_hash(segment)
            if not (story_changed or shots_changed):
                continue
            mark_content_dirty(
                segment,
                "shots_dirty" if shots_changed else "story_dirty",
                source="external_hash_drift",
            )
            changed = True
        return (drifted, True) if changed else (value, False)

    migrated = copy.deepcopy(value)
    migrated.setdefault("outline_attempts", [])
    migrated.setdefault("script_attempt_log", [])
    migrated["authoring_confirmation"] = empty_authoring_confirmation()
    targets = build_segment_story_targets(
        migrated.get("outline") or [],
        list(migrated.get("ending_requirements") or []),
        len(migrated.get("segments") or []),
    )
    for segment, target in zip(migrated.get("segments") or [], targets):
        if not isinstance(segment, dict):
            continue
        frames = int(segment.get("frames") or 0)
        if version == 1:
            legacy_action = _clean_text(segment.pop("action", ""), limit=40_000)
            if legacy_action and frames > 0:
                segment["beats"] = normalize_beats(
                    [{"start_seconds": 0, "end_seconds": canonical_beat_seconds(frames), "action": legacy_action}],
                    frames=frames,
                )
                segment["legacy_action"] = legacy_action
                segment["timeline_state"] = "needs_review"
            else:
                segment.setdefault("beats", [])
                segment["timeline_state"] = "invalid" if not segment["beats"] else "needs_review"
        missing_confirmation = (
            not segment.get("story_target")
            or "covered_outline_chapters" not in segment
            or "fulfilled_ending_requirements" not in segment
        )
        segment["story_target"] = copy.deepcopy(target)
        segment["covered_outline_chapters"] = list(target["chapter_numbers"])
        segment["fulfilled_ending_requirements"] = list(target["required_ending_conditions"])
        if missing_confirmation and segment.get("beats"):
            segment["timeline_state"] = "needs_review"
        segment.setdefault("script_attempts", [])
        card = _story_card_from_existing(segment, target)
        segment["story_card"] = card
        segment["single_workspace"] = _workspace_from_existing(segment, card)
        workspace = segment["single_workspace"]
        legacy_state = (
            "shots_dirty"
            if isinstance(workspace.get("script"), dict)
            and not (
                segment.get("prompt_state") == "valid"
                and workspace.get("state") == "valid"
            )
            else "clean"
        )
        segment["content_sync"] = make_content_sync(
            segment,
            state=legacy_state,
            source="schema_migration",
        )
    migrated["schema_version"] = SCHEMA_VERSION
    return migrated, True


def validate_project(project: Any) -> dict[str, Any]:
    if not isinstance(project, dict):
        raise LongFormError("项目必须是 JSON 对象。")
    if int(project.get("schema_version") or 0) != SCHEMA_VERSION:
        raise LongFormError("项目 schema 版本不受支持。", "project_schema_invalid")
    project_id = str(project.get("id") or "")
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise LongFormError("项目 ID 无效。", "project_id_invalid")
    if project.get("status") not in PROJECT_STATES:
        raise LongFormError("项目状态无效。", "project_state_invalid")
    confirmation = project.get("authoring_confirmation")
    if not isinstance(confirmation, dict):
        raise LongFormError("项目缺少创作确认状态。", "authoring_confirmation_invalid")
    if confirmation.get("state") not in AUTHORING_CONFIRMATION_STATES:
        raise LongFormError("创作确认状态无效。", "authoring_confirmation_invalid")
    fingerprint = str(confirmation.get("fingerprint") or "")
    if fingerprint and not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise LongFormError("创作确认指纹无效。", "authoring_confirmation_invalid")
    for name in ("confirmed_at", "provider", "model"):
        if not isinstance(confirmation.get(name, ""), str):
            raise LongFormError("创作确认元数据无效。", "authoring_confirmation_invalid")
    segments = project.get("segments")
    if not isinstance(segments, list) or not segments:
        raise LongFormError("项目没有分段。", "project_segments_invalid")
    total = 0
    previous_index = 0
    for segment in segments:
        if not isinstance(segment, dict):
            raise LongFormError("分段必须是对象。", "project_segments_invalid")
        index = int(segment.get("index") or 0)
        if index != previous_index + 1:
            raise LongFormError("分段编号必须连续。", "project_segments_invalid")
        if segment.get("id") != f"seg_{index:04d}":
            raise LongFormError("分段 ID 与编号不匹配。", "project_segments_invalid")
        story_target = segment.get("story_target")
        if not isinstance(story_target, dict):
            raise LongFormError("分段缺少剧情目标。", "segment_story_target_invalid")
        if int(story_target.get("segment_index") or 0) != index:
            raise LongFormError("分段剧情目标编号不匹配。", "segment_story_target_invalid")
        if int(story_target.get("segment_count") or 0) != len(segments):
            raise LongFormError("分段剧情目标总数不匹配。", "segment_story_target_invalid")
        if bool(story_target.get("must_close_story")) != (index == len(segments)):
            raise LongFormError("最终段剧情目标标记不匹配。", "segment_story_target_invalid")
        story_card = segment.get("story_card")
        workspace = segment.get("single_workspace")
        if not isinstance(story_card, dict) or not isinstance(workspace, dict):
            raise LongFormError(
                "分段缺少剧情卡片或单段工作区。", "project_segment_layers_invalid"
            )
        content_sync = segment.get("content_sync")
        if not isinstance(content_sync, dict):
            raise LongFormError("分段缺少剧情内容同步状态。", "content_sync_invalid")
        if content_sync.get("state") not in CONTENT_SYNC_STATES:
            raise LongFormError("剧情内容同步状态无效。", "content_sync_invalid")
        for hash_name in ("story_hash", "shots_hash"):
            hash_value = str(content_sync.get(hash_name) or "")
            if not re.fullmatch(r"[0-9a-f]{64}", hash_value):
                raise LongFormError("剧情内容同步哈希无效。", "content_sync_invalid")
        if segment.get("script_state") not in {"pending", "failed"} and not str(
            story_card.get("story_text") or ""
        ).strip():
            raise LongFormError("已规划分段缺少剧情正文。", "story_card_invalid")
        if not isinstance(workspace.get("form"), dict):
            raise LongFormError("单段工作区表单无效。", "single_workspace_invalid")
        if workspace.get("state") not in {"empty", "draft", "valid", "stale"}:
            raise LongFormError("单段工作区状态无效。", "single_workspace_invalid")
        revision = workspace.get("revision")
        if not isinstance(revision, int) or revision < 0:
            raise LongFormError("单段工作区版本无效。", "single_workspace_invalid")
        frames = int(segment.get("frames") or 0)
        if frames <= 0:
            raise LongFormError("分段帧数必须大于 0。", "project_segments_invalid")
        boundary = segment.get("boundary_before")
        if boundary not in BOUNDARY_TYPES or (index == 1 and boundary != "start"):
            raise LongFormError("分段边界无效。", "project_segments_invalid")
        timeline_state = str(segment.get("timeline_state") or "invalid")
        if timeline_state not in TIMELINE_STATES:
            raise LongFormError("分段时间轴状态无效。", "project_timeline_invalid")
        beats = segment.get("beats") or []
        if beats:
            normalized = normalize_beats(beats, frames=frames)
            if normalized != beats:
                raise LongFormError(
                    "分段 beats 尚未规范化到 24fps 帧边界。",
                    "project_timeline_not_normalized",
                )
            if timeline_state == "invalid":
                raise LongFormError("非空 beats 不能标记为 invalid。", "project_timeline_invalid")
        elif timeline_state != "invalid":
            raise LongFormError("空 beats 必须标记为 invalid。", "project_timeline_invalid")
        if segment.get("script_state") in {"ready", "possibly_stale"} and not beats:
            raise LongFormError("已生成分段缺少 beats。", "project_timeline_invalid")
        if timeline_state == "valid":
            try:
                covered = [int(item) for item in segment.get("covered_outline_chapters") or []]
            except (TypeError, ValueError) as exc:
                raise LongFormError(
                    "分段章节确认无效。", "segment_story_target_invalid"
                ) from exc
            fulfilled = [
                str(item)
                for item in segment.get("fulfilled_ending_requirements") or []
            ]
            if covered != [int(item) for item in story_target.get("chapter_numbers") or []]:
                raise LongFormError(
                    "分段章节确认与剧情目标不一致。",
                    "segment_story_target_mismatch",
                )
            if fulfilled != [
                str(item)
                for item in story_target.get("required_ending_conditions") or []
            ]:
                raise LongFormError(
                    "分段结尾确认与剧情目标不一致。",
                    "segment_ending_requirements_mismatch",
                )
        if not isinstance(segment.get("script_attempts", []), list):
            raise LongFormError("分段 script_attempts 必须是数组。", "project_segments_invalid")
        total += frames
        previous_index = index
    if total != int(project.get("target_frames") or 0):
        raise LongFormError("分段帧数之和不等于项目总帧数。", "project_frames_invalid")
    return project


class LongProjectStore:
    """Thread-safe, atomic JSON persistence below one runs directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self._lock = threading.RLock()

    def _safe_id(self, project_id: str) -> str:
        value = str(project_id or "")
        if not PROJECT_ID_RE.fullmatch(value):
            raise LongFormError("项目 ID 无效。", "project_id_invalid")
        return value

    def project_dir(self, project_id: str) -> Path:
        candidate = (self.root / self._safe_id(project_id)).resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise LongFormError(
                "项目目录不能通过联接或符号链接越出 runs。",
                "project_path_invalid",
            ) from exc
        return candidate

    def project_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "project.json"

    @contextmanager
    def transaction(self):
        """Serialize a read-modify-write operation for one server process."""

        with self._lock:
            yield

    @staticmethod
    def _atomic_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        fd, temp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            # On Windows, even a very short-lived reader or filesystem scanner
            # can transiently deny replacement of the destination. All studio
            # readers share the store lock; the bounded retry also covers an
            # external scanner without ever looping indefinitely.
            for attempt in range(5):
                try:
                    os.replace(temp_name, path)
                    break
                except PermissionError:
                    if attempt >= 4:
                        raise
                    time.sleep(0.02 * (attempt + 1))
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def save(self, project: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            validate_project(project)
            project["updated_at"] = utc_now()
            self._atomic_json(self.project_path(project["id"]), project)
            return project

    def load(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            path = self.project_path(project_id)
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise LongFormError("长视频项目不存在。", "project_not_found") from exc
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise LongFormError("长视频项目文件损坏。", "project_file_invalid") from exc
            value, _migrated = migrate_project(value)
            return validate_project(value)

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.root.is_dir():
                return []
            result: list[dict[str, Any]] = []
            for path in sorted(self.root.glob("*/project.json"), reverse=True):
                try:
                    path.resolve().relative_to(self.root)
                    value = json.loads(path.read_text(encoding="utf-8"))
                    value, _migrated = migrate_project(value)
                    validate_project(value)
                except (OSError, UnicodeError, json.JSONDecodeError, LongFormError, ValueError):
                    continue
                result.append(
                    {
                        "id": value["id"],
                        "title": value.get("title") or "未命名长视频",
                        "status": value["status"],
                        "actual_seconds": value["actual_seconds"],
                        "segment_count": len(value["segments"]),
                        "updated_at": value.get("updated_at"),
                    }
                )
            return result

    def snapshot(self, project: dict[str, Any], *, reason: str) -> int:
        with self._lock:
            revision = int(project.get("current_revision") or 0) + 1
            snapshot = copy.deepcopy(project)
            snapshot["revision_snapshot"] = {
                "revision": revision,
                "reason": _clean_text(reason, limit=500),
                "created_at": utc_now(),
            }
            snapshot["revision_history"] = []
            path = self.project_dir(project["id"]) / "revisions" / f"{revision:06d}.json"
            self._atomic_json(path, snapshot)
            project["current_revision"] = revision
            project.setdefault("revision_history", []).append(
                {
                    "revision": revision,
                    "reason": _clean_text(reason, limit=500),
                    "created_at": snapshot["revision_snapshot"]["created_at"],
                }
            )
            return revision

    def restore(self, project_id: str, revision: int) -> dict[str, Any]:
        with self._lock:
            current = self.load(project_id)
            path = self.project_dir(project_id) / "revisions" / f"{int(revision):06d}.json"
            try:
                restored = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise LongFormError("指定 revision 不存在或已损坏。", "revision_not_found") from exc
            restored, _migrated = migrate_project(restored)
            self.snapshot(current, reason=f"恢复前快照 revision {revision}")
            history = current.get("revision_history", [])
            restored.pop("revision_snapshot", None)
            restored["revision_history"] = history
            restored["current_revision"] = current["current_revision"]
            restored["status"] = "stale"
            restored["master"] = {}
            restored["scheduler"] = {
                "current_segment": None,
                "current_prompt_id": "",
                "stop_after_current": False,
                "last_error": None,
            }
            return self.save(restored)


def _materialize_workspace(segment: dict[str, Any]) -> None:
    """Mirror an approved single-shot draft into the render-facing fields."""

    workspace = segment["single_workspace"]
    script = workspace.get("script")
    if not isinstance(script, dict) or not isinstance(script.get("shots"), list):
        segment["script_state"] = "planned"
        segment["timeline_state"] = "invalid"
        return
    beats = []
    visuals: list[str] = []
    cameras: list[str] = []
    sounds: list[str] = []
    music: list[str] = []
    dialogue: list[dict[str, str]] = []
    visible_text: list[str] = []
    for shot in script["shots"]:
        if not isinstance(shot, dict):
            raise LongFormError("单段分镜条目无效。", "single_workspace_invalid")
        action = _clean_text(shot.get("action"), limit=20_000)
        if not action:
            action = _clean_text(shot.get("visual"), limit=20_000)
        beats.append(
            {
                "start_seconds": shot.get("start"),
                "end_seconds": shot.get("end"),
                "action": action,
            }
        )
        for target, value in (
            (visuals, shot.get("visual")),
            (cameras, shot.get("camera")),
            (sounds, shot.get("sound")),
            (music, shot.get("music")),
        ):
            cleaned = _clean_text(value, limit=20_000)
            if cleaned and cleaned not in target:
                target.append(cleaned)
        dialogue.extend(normalize_dialogue(shot.get("dialogue")))
        for text in _clean_text_list(shot.get("visible_text")):
            if text not in visible_text:
                visible_text.append(text)
    normalized_beats = normalize_beats(beats, frames=int(segment["frames"]))
    card = segment["story_card"]
    segment["summary"] = _clean_text(script.get("logline"), limit=20_000) or card["story_text"]
    segment["visual"] = "\n".join(visuals) or card["story_text"]
    segment["beats"] = normalized_beats
    segment["camera"] = "\n".join(cameras)
    segment["dialogue"] = dialogue
    segment["visible_text"] = visible_text
    segment["sound"] = "\n".join(sounds)
    segment["music"] = "\n".join(music)
    segment["present_characters"] = list(card.get("present_characters") or [])
    segment["continuity_in"] = str(card.get("opening_state") or "")
    segment["continuity_out"] = str(card.get("ending_state") or "")
    segment["covered_outline_chapters"] = list(
        segment["story_target"].get("chapter_numbers") or []
    )
    segment["fulfilled_ending_requirements"] = list(
        segment["story_target"].get("required_ending_conditions") or []
    )
    segment["timeline_state"] = "valid"
    segment["script_state"] = "ready"
    prompt = _clean_text(workspace.get("prompt"), limit=200_000)
    validation = workspace.get("validation") or {}
    segment["h3_prompt"] = prompt
    segment["prompt_warnings"] = _clean_text_list(workspace.get("warnings"))
    segment["prompt_state"] = (
        "valid" if prompt and validation.get("valid") is True else "pending"
    )


def save_segment_workspace(
    store: LongProjectStore,
    project: dict[str, Any],
    segment_id: str,
    workspace: dict[str, Any],
    *,
    expected_revision: int | None = None,
    usage_delta: dict[str, Any] | None = None,
    snapshot_reason: str = "",
    mark_script_change_dirty: bool = True,
) -> dict[str, Any]:
    if not isinstance(workspace, dict) or not isinstance(workspace.get("form"), dict):
        raise LongFormError("单段工作区数据无效。", "single_workspace_invalid")
    if len(json.dumps(workspace, ensure_ascii=False)) > 2_000_000:
        raise LongFormError("单段工作区数据过大。", "single_workspace_invalid")
    with store.transaction():
        # Reload under the same lock used for the final replacement. This makes
        # expected_revision a real compare-and-swap instead of comparing with
        # a stale object loaded before another request committed.
        current_project = store.load(str(project.get("id") or ""))
        target = next(
            (item for item in current_project["segments"] if item["id"] == segment_id),
            None,
        )
        if target is None:
            raise LongFormError("分段不存在。", "segment_not_found")
        current_revision = int(
            (target.get("single_workspace") or {}).get("revision") or 0
        )
        previous_shots_hash = shot_source_hash(target)
        if expected_revision is not None and int(expected_revision) != current_revision:
            raise LongFormError(
                f"工作区已在其他页面更新（当前版本 {current_revision}），请重新载入后再保存。",
                "single_workspace_conflict",
            )
        clean = copy.deepcopy(workspace)
        clean["revision"] = current_revision + 1
        clean["updated_at"] = utc_now()
        clean.setdefault("state", "draft")
        clean.setdefault("pictures", {})
        clean.setdefault(
            "validation", {"valid": False, "errors": [], "warnings": []}
        )
        clean.setdefault("warnings", [])
        clean.setdefault("usage", {})
        target["single_workspace"] = clean
        _materialize_workspace(target)
        script_changed = previous_shots_hash != shot_source_hash(target)
        if script_changed and mark_script_change_dirty:
            clean["prompt"] = ""
            clean["state"] = "stale"
            clean["validation"] = {
                "valid": False,
                "errors": [
                    "Shot 已修改：请先点击“同步本段剧情状态”，再重新编译 H3 提示词。"
                ],
                "warnings": [],
            }
            target["h3_prompt"] = ""
            target["prompt_state"] = "stale"
            target["prompt_warnings"] = []
            mark_content_dirty(target, "shots_dirty", source="manual_shot_edit")
            for downstream in current_project["segments"][int(target["index"]) :]:
                downstream_workspace = downstream.get("single_workspace") or {}
                downstream_workspace["state"] = "stale"
                downstream["prompt_state"] = "stale"
                if downstream.get("script_state") == "ready":
                    downstream["script_state"] = "possibly_stale"
                downstream["render_state"] = (
                    "stale" if downstream.get("artifacts") else "pending"
                )
                mark_content_dirty(
                    downstream,
                    "story_dirty",
                    source=f"upstream_shot_{target['index']}_changed",
                )
        elif script_changed:
            target["content_sync"] = make_content_sync(
                target,
                state="clean",
                source="generated_shots",
            )
        target["render_state"] = "stale" if target.get("artifacts") else "pending"
        current_project["master"] = {}
        current_project["stale_from"] = min(
            int(current_project.get("stale_from") or target["index"]),
            target["index"],
        )
        if usage_delta:
            merge_usage(current_project.setdefault("usage", {}), usage_delta)
        all_ready = all(
            item.get("script_state") == "ready"
            and item.get("prompt_state") == "valid"
            and content_sync_is_current(item)
            for item in current_project["segments"]
        )
        current_project["status"] = "ready" if all_ready else "draft"
        saved = store.save(current_project)
        if snapshot_reason:
            store.snapshot(saved, reason=snapshot_reason)
            saved = store.save(saved)
        return saved


def edit_story_card(
    store: LongProjectStore,
    project: dict[str, Any],
    segment_id: str,
    changes: dict[str, Any],
    *,
    confirm_invalidate: bool = False,
) -> dict[str, Any]:
    with store.transaction():
        current = store.load(str(project.get("id") or ""))
        return _edit_story_card_locked(
            store,
            current,
            segment_id,
            changes,
            confirm_invalidate=confirm_invalidate,
        )


def _edit_story_card_locked(
    store: LongProjectStore,
    project: dict[str, Any],
    segment_id: str,
    changes: dict[str, Any],
    *,
    confirm_invalidate: bool = False,
) -> dict[str, Any]:
    if not isinstance(changes, dict):
        raise LongFormError("剧情卡片修改无效。", "story_card_invalid")
    target = next((item for item in project["segments"] if item["id"] == segment_id), None)
    if target is None:
        raise LongFormError("分段不存在。", "segment_not_found")
    original = copy.deepcopy(project)
    card = copy.deepcopy(target["story_card"])
    # Only the Chinese story body and explicit cut/continuation decision are
    # user-authoritative here.  Opening state describes the preceding accepted
    # tail and must never be silently rewritten.  Title, ending state,
    # characters and exact dialogue are reconciled explicitly from the story
    # plus Shot layer.
    if "story_text" in changes:
        card["story_text"] = _clean_text(changes["story_text"], limit=40_000)
    if "boundary_before" in changes:
        boundary = _clean_text(changes["boundary_before"], limit=30).lower()
        if target["index"] == 1:
            boundary = "start"
        elif boundary not in {"continuous", "cut"}:
            raise LongFormError("边界必须是 continuous 或 cut。", "story_card_invalid")
        card["boundary_before"] = boundary
    if not card.get("story_text") or not card.get("ending_state"):
        raise LongFormError("剧情正文和结尾状态不能为空。", "story_card_invalid")
    if card == target["story_card"]:
        return project
    affected = project["segments"][int(target["index"]) - 1 :]
    would_discard_valid_work = any(
        item.get("prompt_state") == "valid"
        or (item.get("single_workspace") or {}).get("state") == "valid"
        for item in affected
    )
    if would_discard_valid_work and not confirm_invalidate:
        raise LongFormError(
            "剧情修改会使本段及后续已保存的分镜/H3 提示词失效，请明确确认后重试。",
            "story_card_invalidation_confirmation_required",
        )
    store.snapshot(original, reason=f"修改剧情卡片 {segment_id} 前")
    target["story_card"] = card
    target["boundary_before"] = card["boundary_before"]
    target["summary"] = card["story_text"]
    target["dialogue"] = copy.deepcopy(card["dialogue"])
    target["continuity_in"] = card["opening_state"]
    target["continuity_out"] = card["ending_state"]
    target["present_characters"] = list(card["present_characters"])
    workspace = target.get("single_workspace")
    if isinstance(workspace, dict):
        form = workspace.setdefault("form", {})
        form["creative_brief"] = card["story_text"]
        form["action_timeline"] = card["story_text"]
        form["exact_dialogue"] = "\n".join(
            item["text"] for item in card["dialogue"]
        )
        form["duration"] = int(target["frames"]) / FPS
        if target["index"] > 1 and card["boundary_before"] == "continuous":
            form["mode"] = (
                form.get("mode")
                if form.get("mode") in {"I2VA", "FL2VA"}
                else "I2VA"
            )
            form["picture1_description"] = card["opening_state"]
            workspace.setdefault("pictures", {}).setdefault("picture1", {})[
                "source"
            ] = "auto_tail"
        elif card["boundary_before"] in {"start", "cut"}:
            picture1 = workspace.setdefault("pictures", {}).setdefault(
                "picture1", {}
            )
            if picture1.get("source") == "auto_tail":
                picture1.update(
                    {"source": "none", "input_path": "", "temporary_name": ""}
                )
                form["mode"] = "T2VA"
                form["picture1_description"] = ""
        workspace["prompt"] = ""
        workspace["state"] = "stale"
        workspace["validation"] = {
            "valid": False,
            "errors": [
                "剧情正文已修改：请先同步本段剧情状态，再重新生成分镜并编译。"
            ],
            "warnings": [],
        }
        target["h3_prompt"] = ""
        target["prompt_warnings"] = []
    first = target["index"]
    for item in project["segments"][first - 1 :]:
        item_workspace = item["single_workspace"]
        item_workspace["state"] = "stale"
        item_workspace["revision"] = int(item_workspace.get("revision") or 0) + 1
        item_workspace["updated_at"] = utc_now()
        item["prompt_state"] = "stale"
        item["render_state"] = "stale" if item.get("artifacts") else "pending"
        if item["index"] == first:
            item["script_state"] = (
                "possibly_stale"
                if isinstance(item_workspace.get("script"), dict)
                else "planned"
            )
            mark_content_dirty(item, "story_dirty", source="manual_story_edit")
        else:
            if item.get("script_state") == "ready":
                item["script_state"] = "possibly_stale"
            mark_content_dirty(
                item,
                "story_dirty",
                source=f"upstream_story_{first}_changed",
            )
    project["status"] = "stale"
    project["stale_from"] = first
    project["master"] = {}
    return store.save(project)


def _split_story_text(text: str) -> tuple[str, str]:
    clean = _clean_text(text, limit=40_000)
    candidates = [match.end() for match in re.finditer(r"[。！？；\n]", clean)]
    middle = len(clean) // 2
    point = min(candidates, key=lambda value: abs(value - middle)) if candidates else middle
    return clean[:point].strip() or clean, clean[point:].strip() or "续写本段剧情。"


def mutate_timeline(
    store: LongProjectStore,
    project: dict[str, Any],
    *,
    operation: str,
    segment_id: str,
    destination_index: int | None = None,
) -> dict[str, Any]:
    segments = copy.deepcopy(project["segments"])
    position = next((i for i, item in enumerate(segments) if item["id"] == segment_id), None)
    if position is None:
        raise LongFormError("分段不存在。", "segment_not_found")
    earliest = position
    if operation == "add_after":
        source = copy.deepcopy(segments[position])
        card = copy.deepcopy(source["story_card"])
        card.update(
            {
                "title": "新增剧情段",
                "story_text": "请在这里填写新增剧情。",
                "dialogue": [],
                "opening_state": source["story_card"].get("ending_state") or "",
                "ending_state": "新增剧情结束状态待确认。",
                "present_characters": [],
                "boundary_before": "continuous",
            }
        )
        source = apply_story_card(source, card, provenance="manual")
        segments.insert(position + 1, source)
    elif operation == "delete":
        if len(segments) <= 1:
            raise LongFormError("项目至少保留一个分段。", "timeline_operation_invalid")
        segments.pop(position)
    elif operation == "move_up":
        if position == 0:
            return project
        segments[position - 1], segments[position] = segments[position], segments[position - 1]
        earliest = position - 1
    elif operation == "move_down":
        if position >= len(segments) - 1:
            return project
        segments[position], segments[position + 1] = segments[position + 1], segments[position]
    elif operation == "move_to":
        try:
            destination = int(destination_index or 0) - 1
        except (TypeError, ValueError) as exc:
            raise LongFormError(
                "拖动目标位置无效。", "timeline_operation_invalid"
            ) from exc
        if destination < 0 or destination >= len(segments):
            raise LongFormError("拖动目标位置无效。", "timeline_operation_invalid")
        if destination == position:
            return project
        moved = segments.pop(position)
        segments.insert(destination, moved)
        earliest = min(position, destination)
    elif operation == "split":
        first_text, second_text = _split_story_text(segments[position]["story_card"]["story_text"])
        first = segments[position]
        second = copy.deepcopy(first)
        first["story_card"]["story_text"] = first_text
        first["story_card"]["ending_state"] = "拆分点的连续状态待确认。"
        second["story_card"]["title"] = str(first["story_card"].get("title") or "本段") + "（下）"
        second["story_card"]["story_text"] = second_text
        second["story_card"]["opening_state"] = first["story_card"]["ending_state"]
        second["story_card"]["dialogue"] = []
        first = apply_story_card(first, first["story_card"], provenance="manual")
        second = apply_story_card(second, second["story_card"], provenance="manual")
        segments[position : position + 1] = [first, second]
    elif operation == "merge_next":
        if position >= len(segments) - 1:
            raise LongFormError("最后一段没有可合并的后段。", "timeline_operation_invalid")
        first, second = segments[position], segments[position + 1]
        card = copy.deepcopy(first["story_card"])
        card["story_text"] = (card.get("story_text", "") + "\n" + second["story_card"].get("story_text", "")).strip()
        card["ending_state"] = second["story_card"].get("ending_state") or card.get("ending_state")
        card["dialogue"] = normalize_dialogue(
            list(card.get("dialogue") or []) + list(second["story_card"].get("dialogue") or [])
        )
        card["present_characters"] = list(
            dict.fromkeys(
                list(card.get("present_characters") or [])
                + list(second["story_card"].get("present_characters") or [])
            )
        )
        segments[position] = apply_story_card(first, card, provenance="manual")
        segments.pop(position + 1)
    else:
        raise LongFormError("未知的时间线操作。", "timeline_operation_invalid")

    store.snapshot(project, reason=f"时间线操作 {operation} 前")
    preserve = range(max(0, earliest))
    segments = reindex_and_allocate(
        segments,
        total_frames=int(project["target_frames"]),
        preserve_positions=preserve,
    )
    targets = build_segment_story_targets(
        project["outline"], project["ending_requirements"], len(segments)
    )
    for offset, (item, target) in enumerate(zip(segments, targets)):
        item["story_target"] = target
        item["story_card"]["boundary_before"] = "start" if offset == 0 else item["story_card"].get("boundary_before", "continuous")
        item["boundary_before"] = item["story_card"]["boundary_before"]
        item["story_card"]["covered_outline_chapters"] = list(target["chapter_numbers"])
        item["story_card"]["fulfilled_ending_requirements"] = list(target["required_ending_conditions"])
        item["covered_outline_chapters"] = list(target["chapter_numbers"])
        item["fulfilled_ending_requirements"] = list(target["required_ending_conditions"])
        item["single_workspace"]["form"]["duration"] = item["frames"] / FPS
        if offset >= earliest:
            item["single_workspace"]["state"] = "stale"
            item["script_state"] = "planned"
            item["prompt_state"] = "stale"
            item["timeline_state"] = "invalid"
            item["beats"] = []
            item["h3_prompt"] = ""
            item["render_state"] = "stale" if item.get("artifacts") else "pending"
            mark_content_dirty(
                item,
                "story_dirty",
                source=f"timeline_{operation}",
            )
    project["segments"] = segments
    project["status"] = "stale"
    project["stale_from"] = earliest + 1
    project["master"] = {}
    return store.save(project)


EDITABLE_SEGMENT_FIELDS = {
    "boundary_before",
    "summary",
    "visual",
    "beats",
    "camera",
    "dialogue",
    "visible_text",
    "sound",
    "music",
    "present_characters",
    "continuity_in",
    "continuity_out",
    "extra_constraints",
}


def edit_segment(
    store: LongProjectStore,
    project: dict[str, Any],
    segment_id: str,
    changes: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(changes, dict):
        raise LongFormError("分段修改必须是对象。", "segment_edit_invalid")
    target = next((item for item in project["segments"] if item["id"] == segment_id), None)
    if target is None:
        raise LongFormError("分段不存在。", "segment_not_found")
    original = copy.deepcopy(project)
    for name, value in changes.items():
        if name not in EDITABLE_SEGMENT_FIELDS:
            continue
        if name == "dialogue":
            target[name] = normalize_dialogue(value)
        elif name == "beats":
            target["beats"] = normalize_beats(value, frames=int(target["frames"]))
            target["timeline_state"] = "valid"
            target["legacy_action"] = ""
            story_target = target.get("story_target") or {}
            target["covered_outline_chapters"] = [
                int(item) for item in story_target.get("chapter_numbers") or []
            ]
            target["fulfilled_ending_requirements"] = [
                str(item)
                for item in story_target.get("required_ending_conditions") or []
            ]
        elif name in {"visible_text", "present_characters"}:
            target[name] = _clean_text_list(value)
        elif name == "boundary_before":
            boundary = _clean_text(value, limit=30).lower()
            if target["index"] == 1:
                boundary = "start"
            elif boundary not in {"continuous", "cut"}:
                raise LongFormError("边界必须是 continuous 或 cut。", "segment_edit_invalid")
            target[name] = boundary
        else:
            target[name] = _clean_text(value, limit=40_000)
    missing_fields = [
        name
        for name in ("summary", "visual", "continuity_out")
        if not str(target.get(name) or "").strip()
    ]
    if not target.get("beats"):
        missing_fields.append("beats")
    if missing_fields:
        raise LongFormError(
            "手动分段缺少必填内容：" + "、".join(missing_fields),
            "segment_edit_invalid",
        )
    store.snapshot(original, reason=f"手动修改 {segment_id} 前")
    project["current_revision"] = original["current_revision"]
    project["revision_history"] = original["revision_history"]
    target["provenance"] = "manual"
    target["manual_revision"] = int(target.get("manual_revision") or 0) + 1
    target["script_state"] = "ready"
    first_stale = target["index"]
    for item in project["segments"][first_stale - 1 :]:
        item["prompt_state"] = "stale"
        (item.get("single_workspace") or {})["state"] = "stale"
        if item.get("render_state") not in {"pending", "stale"}:
            item["render_state"] = "stale"
        mark_content_dirty(
            item,
            "shots_dirty" if item["index"] == first_stale else "story_dirty",
            source=(
                "manual_segment_edit"
                if item["index"] == first_stale
                else f"upstream_segment_{first_stale}_changed"
            ),
        )
    for item in project["segments"][first_stale:]:
        if item.get("script_state") == "ready":
            item["script_state"] = "possibly_stale"
    project["stale_from"] = first_stale
    project["status"] = "stale"
    project["master"] = {}
    return store.save(project)


def build_regeneration_plan(
    project: dict[str, Any],
    *,
    edited_index: int,
    keep_segment_ids: Iterable[str],
    duration_policy: str,
) -> dict[str, Any]:
    segments = project["segments"]
    if edited_index < 1 or edited_index >= len(segments):
        raise LongFormError("没有可重生成的后续分段。", "regeneration_range_invalid")
    if duration_policy not in {"fixed", "replan"}:
        raise LongFormError("时长策略必须是 fixed 或 replan。", "duration_policy_invalid")
    keep = {str(item) for item in keep_segment_ids}
    valid_after = {item["id"] for item in segments[edited_index:]}
    if not keep.issubset(valid_after):
        raise LongFormError("保留列表包含重生成范围外的分段。", "regeneration_anchor_invalid")
    actions = []
    for item in segments[edited_index:]:
        actions.append(
            {
                "segment_id": item["id"],
                "index": item["index"],
                "action": "keep" if item["id"] in keep else "rewrite",
                "manual": item.get("provenance") == "manual",
                "frames": item["frames"],
            }
        )
    return {
        "edited_index": edited_index,
        "rewrite_from": edited_index + 1,
        "duration_policy": duration_policy,
        "target_frames": project["target_frames"],
        "actions": actions,
        "anchor_ids": [item["segment_id"] for item in actions if item["action"] == "keep"],
    }


def reindex_and_allocate(
    segments: list[dict[str, Any]],
    *,
    total_frames: int,
    preserve_positions: Iterable[int] = (),
) -> list[dict[str, Any]]:
    if not segments:
        raise LongFormError("重规划后不能没有分段。", "regeneration_empty")
    preserved = {int(position) for position in preserve_positions}
    if any(position < 0 or position >= len(segments) for position in preserved):
        raise LongFormError("保留分段位置无效。", "regeneration_anchor_invalid")
    fixed_frames = sum(int(segments[position].get("frames") or 0) for position in preserved)
    mutable_positions = [index for index in range(len(segments)) if index not in preserved]
    remaining_frames = int(total_frames) - fixed_frames
    if remaining_frames < 0:
        raise LongFormError("保留锚点总时长超过项目总时长。", "segment_count_invalid")
    assigned: dict[int, int] = {
        position: int(segments[position]["frames"]) for position in preserved
    }
    if mutable_positions:
        base, extra = divmod(remaining_frames, len(mutable_positions))
        values = [base + (1 if index < extra else 0) for index in range(len(mutable_positions))]
        if any(value < MIN_RELIABLE_OUTPUT_FRAMES for value in values):
            raise LongFormError(
                "保留锚点后，待重写分段不足约 5 秒；请减少段数或保留项。",
                "segment_count_invalid",
            )
        if any(value > MAX_RELIABLE_OUTPUT_FRAMES for value in values):
            raise LongFormError(
                "保留锚点后，待重写分段超过 15 秒；请增加段数。",
                "segment_count_invalid",
            )
        assigned.update(zip(mutable_positions, values))
    elif remaining_frames != 0:
        raise LongFormError(
            "所有后续段都被保留，无法重新分配剩余时长。", "segment_count_invalid"
        )
    for position, item in enumerate(segments):
        frames = assigned[position]
        index = position + 1
        item["id"] = f"seg_{index:04d}"
        item["index"] = index
        item["frames"] = frames
        item["duration_seconds"] = frames / FPS
        item["duration_display"] = format_seconds(frames)
        if position not in preserved:
            item["beats"] = []
            item["covered_outline_chapters"] = []
            item["fulfilled_ending_requirements"] = []
            item["timeline_state"] = "invalid"
            item["legacy_action"] = ""
            item["script_attempts"] = []
            item["script_state"] = "planned" if item.get("story_card") else "pending"
            item["prompt_state"] = "stale"
            item["h3_prompt"] = ""
            workspace = item.get("single_workspace")
            if isinstance(workspace, dict):
                workspace["state"] = "stale"
                if isinstance(workspace.get("form"), dict):
                    workspace["form"]["duration"] = frames / FPS
                workspace["prompt"] = ""
                workspace["validation"] = {
                    "valid": False,
                    "errors": ["时间线结构已改变，需要重新编译。"],
                    "warnings": [],
                }
        if index == 1:
            item["boundary_before"] = "start"
        if isinstance(item.get("story_card"), dict):
            item["story_card"]["boundary_before"] = item["boundary_before"]
    return segments


def merge_usage(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in (incoming or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            target[key] = target.get(key, 0) + value
        elif isinstance(value, dict):
            child = target.setdefault(key, {})
            if isinstance(child, dict):
                merge_usage(child, value)
