"""Background local-model and ComfyUI orchestration for long-form H3 projects."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import random
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from longform import (
    FPS,
    LongFormError,
    LongProjectStore,
    aggregate_shot_dialogue,
    apply_story_card,
    allocate_segment_frames,
    authoring_confirmation_is_current,
    build_regeneration_plan,
    build_segment_story_targets,
    content_sync_is_current,
    ensure_required_dialogue,
    exact_dialogue_lines,
    make_project,
    make_content_sync,
    mark_content_dirty,
    merge_usage,
    normalize_outline_result,
    normalize_segment_result,
    normalize_story_card_result,
    reindex_and_allocate,
    save_segment_workspace,
    shot_source_hash,
    story_source_hash,
    utc_now,
    validate_project,
)
from tools.build_long_workflow import (
    AUDIO_VAE,
    TEXT_ENCODER,
    TURBO_LORA,
    UPSCALE_MODEL,
    VIDEO_MODEL,
    VIDEO_VAE,
    build_api_workflow,
    validate_api_workflow,
)
from project_assets import MIME_BY_EXTENSION, ProjectAssetStore


ProviderCall = Callable[..., tuple[dict[str, Any], dict[str, Any], str | None]]
SegmentPrecompiler = Callable[
    [dict[str, Any], dict[str, Any], Callable[[list[dict[str, Any]]], tuple[dict[str, Any], dict[str, Any]]]],
    tuple[dict[str, Any], dict[str, Any]],
]

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
RETRYABLE_PROVIDER_CODES = {
    "provider_timeout",
    "provider_http_429",
    "provider_http_500",
    "provider_http_503",
    "provider_content_empty",
}


def read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise LongFormError(f"无法读取运行文件：{path.name}", "runtime_file_invalid") from exc


def _json_user(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _dialogue_texts(segments: list[dict[str, Any]]) -> set[str]:
    return {
        str(item.get("text") or "")
        for segment in segments
        for item in segment.get("dialogue") or []
        if isinstance(item, dict) and str(item.get("text") or "")
    }


def _provider_error(exc: Exception) -> tuple[str, str]:
    return str(getattr(exc, "message", "模型请求失败。")), str(
        getattr(exc, "code", "provider_error")
    )


def compute_render_readiness(
    project: dict[str, Any], *, require_authoring_confirmation: bool = True
) -> dict[str, Any]:
    """Return the single authoritative pre-GPU readiness report."""

    blockers: list[dict[str, Any]] = []
    segment_reports: list[dict[str, Any]] = []
    for segment in project.get("segments") or []:
        index = int(segment.get("index") or 0)
        workspace = segment.get("single_workspace") or {}
        checks = {
            "content_sync": content_sync_is_current(segment),
            "script": segment.get("script_state") == "ready",
            "timeline": segment.get("timeline_state") == "valid",
            "prompt": bool(segment.get("h3_prompt"))
            and segment.get("prompt_state") == "valid",
            "workspace": workspace.get("state") == "valid"
            and (workspace.get("validation") or {}).get("valid") is True,
        }
        labels = {
            "content_sync": "剧情正文、Shot 与首尾状态尚未同步",
            "script": "单段分镜尚未保存完成",
            "timeline": "时间轴尚未通过校验",
            "prompt": "H3 提示词尚未有效落盘",
            "workspace": "单段工作区尚未确认保存",
        }
        for name, ok in checks.items():
            if not ok:
                blockers.append(
                    {
                        "code": f"segment_{name}_not_ready",
                        "segment_index": index,
                        "message": f"第 {index} 段：{labels[name]}。",
                    }
                )
        segment_reports.append(
            {
                "segment_index": index,
                "segment_id": segment.get("id"),
                "checks": checks,
                "workspace_revision": int(workspace.get("revision") or 0),
            }
        )
    try:
        ensure_required_dialogue(project)
    except LongFormError as exc:
        blockers.append(
            {"code": exc.code, "segment_index": None, "message": exc.message}
        )
    confirmation = project.get("authoring_confirmation") or {}
    confirmation_current = authoring_confirmation_is_current(project)
    if require_authoring_confirmation and not confirmation_current:
        blockers.append(
            {
                "code": "authoring_confirmation_required",
                "segment_index": None,
                "message": (
                    "剧本或 H3 提示词尚未按当前内容确认。请先保存全部内容，再点击"
                    "“确认剧本与全部 H3 提示词完成并释放显存”。"
                ),
            }
        )
    return {
        "ready": not blockers,
        "blockers": blockers,
        "segments": segment_reports,
        "authoring_confirmation": {
            "state": str(confirmation.get("state") or "unconfirmed"),
            "current": confirmation_current,
            "confirmed_at": str(confirmation.get("confirmed_at") or ""),
            "provider": str(confirmation.get("provider") or ""),
            "model": str(confirmation.get("model") or ""),
        },
        "ready_segments": sum(
            1 for item in segment_reports if all(item["checks"].values())
        ),
        "total_segments": len(segment_reports),
    }


class SegmentRepairExhausted(LongFormError):
    def __init__(self, segment_index: int, attempts: list[dict[str, Any]]) -> None:
        last_error = attempts[-1].get("validation_error") or {}
        detail = str(last_error.get("message") or "分段格式或时间轴无效。")
        super().__init__(
            f"第 {segment_index} 段经过两次自动修复后仍无效：{detail}",
            "segment_semantic_repair_exhausted",
        )
        self.attempts = attempts


@dataclass
class BackgroundTask:
    id: str
    kind: str
    project_id: str
    state: str = "queued"
    stage: str = ""
    message: str = ""
    current: int = 0
    total: int = 0
    error: dict[str, Any] | None = None
    started_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    stop_requested: bool = False
    thinking_text: str = ""
    live_text: str = ""
    result: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "project_id": self.project_id,
            "state": self.state,
            "stage": self.stage,
            "message": self.message,
            "current": self.current,
            "total": self.total,
            "error": self.error,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "stop_requested": self.stop_requested,
            "thinking_text": self.thinking_text[-12000:],
            "live_text": self.live_text[-6000:],
            "result": copy.deepcopy(self.result),
        }


def ensure_media_backend() -> None:
    """Fail before GPU submission when the Studio Python cannot inspect media."""

    try:
        importlib.import_module("av")
    except (ImportError, OSError) as exc:
        raise LongFormError(
            "视频生产需要 Studio 的 Python 安装 PyAV。请先执行 "
            ".venv\\Scripts\\python.exe -m pip install av，再重启 Studio。",
            "media_dependency_missing",
        ) from exc


def _comfy_combo_choices(descriptor: Any) -> tuple[Any, ...] | None:
    """Return options from both legacy and ComfyUI 0.30 COMBO schemas."""

    if not isinstance(descriptor, (list, tuple)) or not descriptor:
        return None
    legacy_choices = descriptor[0]
    if isinstance(legacy_choices, (list, tuple)):
        return tuple(legacy_choices)
    if (
        legacy_choices == "COMBO"
        and len(descriptor) > 1
        and isinstance(descriptor[1], dict)
    ):
        options = descriptor[1].get("options")
        if isinstance(options, (list, tuple)):
            return tuple(options)
    return None


class ComfyClient:
    """Small standard-library client that never clears or interrupts the queue."""

    REQUIRED_NODES = {
        "UNETLoader",
        "CLIPLoader",
        "VAELoader",
        "MiniMaxH3HybridRefAndKeyframe",
        "MiniMaxH3TurboLoRA",
        "MiniMaxH3TurboSampler",
        "RandomNoise",
        "BasicScheduler",
        "BasicGuider",
        "SamplerCustomAdvanced",
        "VAEDecode",
        "VAEDecodeAudio",
        "ImageFromBatch",
        "ImageBatch",
        "UpscaleModelLoader",
        "ImageUpscaleWithModel",
        "ImageScale",
        "TrimAudioDuration",
        "CreateVideo",
        "LoadImage",
        "H3Idea2VideoProjectImageSave",
        "H3Idea2VideoProjectVideoSave",
    }
    REQUIRED_MODELS = (
        ("UNETLoader", "unet_name", VIDEO_MODEL),
        ("MiniMaxH3TurboLoRA", "lora_name", TURBO_LORA),
        ("CLIPLoader", "clip_name", TEXT_ENCODER),
        ("VAELoader", "vae_name", VIDEO_VAE),
        ("VAELoader", "vae_name", AUDIO_VAE),
        ("UpscaleModelLoader", "model_name", UPSCALE_MODEL),
    )
    UPSCALE_NODES = {"UpscaleModelLoader", "ImageUpscaleWithModel", "ImageScale"}

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8188",
        *,
        timeout: float = 10.0,
        urlopen_func: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.urlopen = urlopen_func
        self.client_id = "h3-prompt-studio-" + uuid.uuid4().hex

    @staticmethod
    def _raise_http_error(exc: urllib.error.HTTPError) -> None:
        try:
            raw = exc.read(65536)
        except OSError:
            raw = b""
        finally:
            exc.close()
        detail = ""
        try:
            value = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeError, json.JSONDecodeError):
            value = None
        if isinstance(value, dict):
            error = value.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("type") or "")
            elif error:
                detail = str(error)
        if not detail:
            detail = str(exc.reason or "request rejected")
        detail = re.sub(r"\s+", " ", detail).strip()[:800]
        raise LongFormError(
            f"ComfyUI HTTP {int(exc.code)}：{detail}",
            f"comfy_http_{int(exc.code)}",
        ) from exc

    def _request(self, path: str, value: Any | None = None) -> Any:
        data = None
        method = "GET"
        headers = {"Accept": "application/json"}
        if value is not None:
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            method = "POST"
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method, headers=headers
        )
        try:
            with self.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            self._raise_http_error(exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LongFormError(
                f"无法连接 ComfyUI：{self.base_url}。", "comfy_unavailable"
            ) from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise LongFormError("ComfyUI 返回了无效 JSON。", "comfy_response_invalid") from exc

    def _request_bytes(
        self,
        path: str,
        *,
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> bytes:
        headers = {"Accept": "application/json"}
        method = "GET"
        if data is not None:
            method = "POST"
            if content_type:
                headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method, headers=headers
        )
        try:
            with self.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            self._raise_http_error(exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LongFormError(
                f"Cannot connect to ComfyUI: {self.base_url}", "comfy_unavailable"
            ) from exc

    def upload_image(
        self,
        path: Path,
        *,
        subfolder: str,
        filename: str | None = None,
    ) -> dict[str, str]:
        """Upload one project-owned image through ComfyUI's public input API."""

        source = Path(path)
        try:
            payload = source.read_bytes()
        except OSError as exc:
            raise LongFormError("Project image cannot be read.", "project_asset_missing") from exc
        suffix = source.suffix.lower()
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(suffix)
        magic_ok = bool(
            (mime == "image/png" and payload.startswith(b"\x89PNG\r\n\x1a\n"))
            or (mime == "image/jpeg" and payload.startswith(b"\xff\xd8\xff"))
            or (
                mime == "image/webp"
                and payload.startswith(b"RIFF")
                and payload[8:12] == b"WEBP"
            )
        )
        if not magic_ok or not payload or len(payload) > 10 * 1024 * 1024:
            raise LongFormError(
                "Project image is invalid or exceeds 10 MiB.", "project_asset_invalid"
            )
        safe_subfolder = str(subfolder or "").strip().replace("\\", "/").strip("/")
        parts = [part for part in safe_subfolder.split("/") if part]
        if not parts or any(part in {".", ".."} for part in parts):
            raise LongFormError("ComfyUI upload subfolder is invalid.", "comfy_upload_invalid")
        target_name = str(filename or source.name).strip()
        if not target_name or Path(target_name).name != target_name:
            raise LongFormError("ComfyUI upload filename is invalid.", "comfy_upload_invalid")
        boundary = "----H3Idea2Video" + uuid.uuid4().hex
        body = bytearray()

        def field(name: str, value: str) -> None:
            body.extend(f"--{boundary}\r\n".encode("ascii"))
            body.extend(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii")
            )
            body.extend(value.encode("utf-8"))
            body.extend(b"\r\n")

        field("type", "input")
        field("subfolder", "/".join(parts))
        field("overwrite", "true")
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="image"; filename="{target_name}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode("utf-8")
        )
        body.extend(payload)
        body.extend(f"\r\n--{boundary}--\r\n".encode("ascii"))
        raw = self._request_bytes(
            "/upload/image",
            data=bytes(body),
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise LongFormError("ComfyUI upload response is invalid.", "comfy_response_invalid") from exc
        if not isinstance(response, dict) or not response.get("name"):
            raise LongFormError("ComfyUI did not confirm the image upload.", "comfy_upload_failed")
        returned_subfolder = str(response.get("subfolder") or "").replace("\\", "/").strip("/")
        returned_name = str(response["name"])
        return {
            "type": "input",
            "name": "/".join(part for part in (returned_subfolder, returned_name) if part),
            "filename": returned_name,
            "subfolder": returned_subfolder,
        }

    def view_bytes(self, *, name: str, file_type: str = "output") -> bytes:
        normalized = str(name or "").strip().replace("\\", "/")
        parts = [part for part in normalized.split("/") if part]
        if not parts or any(part in {".", ".."} for part in parts):
            raise LongFormError("ComfyUI file reference is invalid.", "comfy_output_invalid")
        query = urllib.parse.urlencode(
            {
                "filename": parts[-1],
                "subfolder": "/".join(parts[:-1]),
                "type": file_type,
            }
        )
        return self._request_bytes("/view?" + query)

    def preflight(
        self,
        info: dict[str, Any] | None = None,
        *,
        require_upscale: bool = True,
    ) -> dict[str, Any]:
        if info is None:
            info = self._request("/object_info")
        if not isinstance(info, dict):
            raise LongFormError("ComfyUI object_info 无效。", "comfy_response_invalid")
        required_nodes = set(self.REQUIRED_NODES)
        if not require_upscale:
            required_nodes.difference_update(self.UPSCALE_NODES)
        missing = sorted(required_nodes - set(info))
        if missing:
            raise LongFormError(
                "ComfyUI 缺少节点："
                + ", ".join(missing)
                + "。请运行 install_comfyui_nodes.bat，并在方便时手动重启 ComfyUI。",
                "comfy_nodes_missing",
            )
        missing_models: list[str] = []
        for node_name, input_name, filename in self.REQUIRED_MODELS:
            if not require_upscale and node_name == "UpscaleModelLoader":
                continue
            try:
                descriptor = info[node_name]["input"]["required"][input_name]
            except (KeyError, IndexError, TypeError) as exc:
                raise LongFormError(
                    f"ComfyUI 节点 {node_name}.{input_name} 的 object_info 无效。",
                    "comfy_response_invalid",
                ) from exc
            choices = _comfy_combo_choices(descriptor)
            if choices is None:
                raise LongFormError(
                    f"ComfyUI 节点 {node_name}.{input_name} 未返回模型选项。",
                    "comfy_response_invalid",
                )
            if filename not in choices:
                missing_models.append(filename)
        if missing_models:
            raise LongFormError(
                "ComfyUI 缺少工作流模型：" + ", ".join(sorted(set(missing_models))),
                "comfy_models_missing",
            )
        return {"ok": True, "missing_nodes": [], "client_id": self.client_id}

    def queue(self) -> dict[str, Any]:
        value = self._request("/queue")
        if not isinstance(value, dict):
            raise LongFormError("ComfyUI 队列响应无效。", "comfy_response_invalid")
        return value

    def wait_until_idle(
        self,
        *,
        stop: Callable[[], bool],
        poll_seconds: float = 2.0,
    ) -> None:
        while True:
            if stop():
                raise LongFormError("任务已请求暂停。", "task_paused")
            queue = self.queue()
            running = queue.get("queue_running") or []
            pending = queue.get("queue_pending") or []
            if not running and not pending:
                return
            time.sleep(poll_seconds)

    def submit(self, workflow: dict[str, Any]) -> str:
        errors = validate_api_workflow(workflow)
        if errors:
            raise LongFormError("API 工作流无效：" + "; ".join(errors), "workflow_invalid")
        response = self._request(
            "/prompt",
            {
                "prompt": workflow["prompt"],
                "client_id": self.client_id,
                "extra_data": {"h3_prompt_studio": workflow.get("meta") or {}},
            },
        )
        if not isinstance(response, dict) or not response.get("prompt_id"):
            error = response.get("error") if isinstance(response, dict) else None
            raise LongFormError(
                "ComfyUI 拒绝了工作流。" + (f" {error}" if error else ""),
                "comfy_prompt_rejected",
            )
        return str(response["prompt_id"])

    def history(self, prompt_id: str) -> dict[str, Any] | None:
        value = self._request("/history/" + urllib.parse.quote(prompt_id, safe=""))
        if not isinstance(value, dict):
            return None
        item = value.get(prompt_id)
        return item if isinstance(item, dict) else None

    def wait_history(
        self,
        prompt_id: str,
        *,
        stop: Callable[[], bool],
        timeout_seconds: float = 6 * 60 * 60,
        poll_seconds: float = 2.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            item = self.history(prompt_id)
            if item is not None:
                status = item.get("status") or {}
                if status.get("status_str") == "error" or status.get("completed") is False:
                    raise LongFormError("ComfyUI 视频任务执行失败。", "comfy_execution_failed")
                return item
            if stop():
                # The current Comfy job is deliberately not interrupted.  Continue
                # watching it, then the scheduler will pause before the next job.
                pass
            time.sleep(poll_seconds)
        raise LongFormError("等待 ComfyUI 任务完成超时。", "comfy_execution_timeout")


def _field_positions(prompt: str, fields: tuple[str, ...]) -> list[int]:
    return [prompt.find(field + ":") for field in fields]


def validate_compiled_prompt(
    prompt: str,
    *,
    segment: dict[str, Any],
    has_first_frame: bool,
    reference_count: int,
) -> list[str]:
    errors: list[str] = []
    text = str(prompt or "").strip()
    if not text:
        return ["提示词为空。"]
    fields = REF_FIELDS if reference_count else BASE_FIELDS
    positions = _field_positions(text, fields)
    if any(value < 0 for value in positions) or positions != sorted(positions):
        errors.append("H3 字段缺失或顺序错误。")
    if any(text.count(field + ":") != 1 for field in fields):
        errors.append("H3 核心字段必须各出现一次。")
    forbidden = BASE_FIELDS if reference_count else REF_FIELDS[:4]
    if reference_count:
        for field in BASE_FIELDS[:1]:
            if field + ":" in text:
                errors.append("Ref2VA 提示词不得包含基础 integrated 字段。")
    else:
        for field in forbidden:
            if field + ":" in text:
                errors.append("基础模式不得包含 Ref2VA 六段式字段。")
        if has_first_frame:
            expected = (
                "For the target video, at 0.00 seconds into the target video, "
                "<Picture 1> (from [Shot 1]) is fully referenced."
            )
            if not text.startswith(expected):
                errors.append("连续基础模式缺少官方 I2VA 首行。")
        elif text.startswith("For the target video") or "reference pictures align" in text[:300]:
            errors.append("T2VA 段不得包含图片对齐指令。")
    duration = int(segment["frames"]) / FPS
    for minutes, seconds in re.findall(r"(\d{2}):(\d{2}(?:\.\d{1,3})?)", text):
        if int(minutes) * 60 + float(seconds) > duration + 1 / FPS:
            errors.append("提示词时间戳超过分段时长。")
            break
    for seconds in re.findall(
        r"(?<![\d:])(\d+(?:\.\d{1,3})?)\s*(?:-second\s+mark|seconds?\s+into)",
        text,
        flags=re.IGNORECASE,
    ):
        if float(seconds) > duration + 1 / FPS:
            errors.append("提示词时间戳超过分段时长。")
            break
    for dialogue in segment.get("dialogue") or []:
        exact = str(dialogue.get("text") or "")
        language = str(dialogue.get("language") or "Chinese")
        if exact and f"<d>[{language}] {exact}</d>" not in text:
            errors.append(f"精确对白未完整保留：{exact}")
    for visible in segment.get("visible_text") or []:
        if str(visible) not in text:
            errors.append(f"可见文字未完整保留：{visible}")
    picture_numbers = [int(value) for value in re.findall(r"<Picture\s+(\d+)>", text)]
    allowed_pictures = reference_count + (1 if has_first_frame and reference_count else 0)
    if reference_count and picture_numbers and max(picture_numbers) > allowed_pictures:
        errors.append("提示词包含未连接的 Picture 引用。")
    if reference_count:
        subject_section = text[: positions[1] if positions[1] >= 0 else len(text)]
        expected_pictures = set(range(1, allowed_pictures + 1))
        defined_pictures = {
            int(value) for value in re.findall(r"<Picture\s+(\d+)>", subject_section)
        }
        if not expected_pictures.issubset(defined_pictures):
            errors.append("subject_definitions 没有定义全部已连接 Picture。")
        all_subjects = {int(value) for value in re.findall(r"<Subject\s+(\d+)>", text)}
        defined_subjects = {
            int(value) for value in re.findall(r"<Subject\s+(\d+)>", subject_section)
        }
        if not all_subjects.issubset(defined_subjects):
            errors.append("提示词包含未定义的 Subject 引用。")
    else:
        allowed_pictures = 1 if has_first_frame else 0
        if picture_numbers and max(picture_numbers) > allowed_pictures:
            errors.append("基础提示词包含未连接的 Picture 引用。")
        if re.search(r"<Subject\s+\d+>", text):
            errors.append("基础提示词不得使用 Ref2VA Subject 引用。")
    dialogue_tags = re.findall(r"<d>.*?</d>", text, flags=re.DOTALL)
    if text.count("<d>") != text.count("</d>") or any(
        not re.fullmatch(r"<d>\[[^\]\r\n]+]\s+.+?</d>", tag, flags=re.DOTALL)
        for tag in dialogue_tags
    ):
        errors.append("对白标签不完整或缺少语言标签。")
    if re.search(r"<(?:Video|Audio)\s+\d+>", text):
        errors.append("长视频工作流没有连接 Video/Audio 参考。")
    return list(dict.fromkeys(errors))


def normalize_compile_result(
    raw: Any,
    *,
    segment: dict[str, Any],
    has_first_frame: bool,
    reference_count: int,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LongFormError("H3 编译结果必须是 JSON 对象。", "long_compile_invalid")
    expected_mode = "Ref2VA" if reference_count else ("I2VA" if has_first_frame else "T2VA")
    if str(raw.get("mode") or "") != expected_mode:
        raise LongFormError(
            f"H3 编译模式错误：期望 {expected_mode}。", "long_compile_mode_mismatch"
        )
    prompt = str(raw.get("prompt") or "")
    errors = validate_compiled_prompt(
        prompt,
        segment=segment,
        has_first_frame=has_first_frame,
        reference_count=reference_count,
    )
    if errors:
        raise LongFormError("；".join(errors), "long_prompt_validation_failed")
    warnings = raw.get("warnings") or []
    if not isinstance(warnings, list):
        warnings = [str(warnings)]
    return {"prompt": prompt, "warnings": [str(item) for item in warnings]}


def _project_artifact_receipt(
    history_item: dict[str, Any],
    node_id: str,
    *,
    project_root: Path,
) -> tuple[dict[str, Any], list[Path]]:
    output = (history_item.get("outputs") or {}).get(str(node_id)) or {}
    strings: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, str):
            strings.append(item)
        elif isinstance(item, dict):
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(output)
    receipt = None
    for item in strings:
        try:
            candidate = json.loads(item)
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            isinstance(candidate, dict)
            and candidate.get("schema") == "h3_idea2video_artifact_receipt_v1"
        ):
            receipt = candidate
            break
    if receipt is None:
        raise LongFormError(
            "ComfyUI project output node returned no artifact receipt.",
            "comfy_output_missing",
        )
    root = Path(project_root).resolve()
    files = receipt.get("files")
    if not isinstance(files, list) or not files:
        raise LongFormError("Project artifact receipt has no files.", "comfy_output_invalid")
    paths: list[Path] = []
    for item in files:
        if not isinstance(item, dict):
            raise LongFormError("Project artifact receipt is invalid.", "comfy_output_invalid")
        relative = str(item.get("relative_path") or "").replace("\\", "/")
        parts = [part for part in relative.split("/") if part]
        if not parts or parts[0] != "runs" or any(part in {".", ".."} for part in parts):
            raise LongFormError("Project artifact path is invalid.", "comfy_output_invalid")
        path = root.joinpath(*parts).resolve()
        try:
            path.relative_to((root / "runs").resolve())
        except ValueError as exc:
            raise LongFormError("Project artifact escaped runs.", "comfy_output_invalid") from exc
        if not path.is_file():
            raise LongFormError("Project artifact is missing.", "comfy_output_missing")
        if path.stat().st_size != int(item.get("bytes") or -1):
            raise LongFormError("Project artifact size mismatch.", "comfy_output_invalid")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != str(item.get("sha256") or ""):
            raise LongFormError("Project artifact hash mismatch.", "comfy_output_invalid")
        paths.append(path)
    return receipt, paths


def inspect_media(
    path: Path,
    *,
    expected_frames: int,
    expected_size: tuple[int, int],
    require_audio: bool = True,
) -> dict[str, Any]:
    """Decode a saved segment and reject truncated video or missing native audio."""

    try:
        import av
    except ImportError as exc:  # pragma: no cover - bundled Comfy environments include PyAV
        raise LongFormError("当前 Python 缺少 ComfyUI 已使用的 PyAV，无法核验视频。", "pyav_missing") from exc
    media_path = Path(path)
    try:
        with av.open(str(media_path)) as container:
            if not container.streams.video:
                raise LongFormError("分段文件没有视频流。", "media_video_missing")
            stream = container.streams.video[0]
            size = (int(stream.codec_context.width), int(stream.codec_context.height))
            frame_count = sum(1 for _frame in container.decode(video=0))
        with av.open(str(media_path)) as container:
            has_audio = bool(container.streams.audio)
            audio_samples = (
                sum(int(frame.samples) for frame in container.decode(audio=0))
                if has_audio
                else 0
            )
    except LongFormError:
        raise
    except Exception as exc:
        raise LongFormError(f"无法解码分段视频：{media_path.name}", "media_decode_failed") from exc
    if frame_count != int(expected_frames):
        raise LongFormError(
            f"分段帧数错误：期望 {expected_frames}，实际 {frame_count}。",
            "media_frame_count_invalid",
        )
    if size != tuple(expected_size):
        raise LongFormError(
            f"分段分辨率错误：期望 {expected_size[0]}×{expected_size[1]}，实际 {size[0]}×{size[1]}。",
            "media_resolution_invalid",
        )
    if require_audio and (not has_audio or audio_samples <= 0):
        raise LongFormError("分段文件没有可解码的模型原生音频。", "media_audio_missing")
    return {
        "path": str(media_path),
        "frames": frame_count,
        "width": size[0],
        "height": size[1],
        "has_audio": has_audio,
        "audio_samples": audio_samples,
    }


def assemble_master_video(
    segment_paths: list[Path],
    destination: Path,
    *,
    expected_frames: int,
) -> dict[str, Any]:
    """Join accepted 1080p segments into one frame-exact H.264/AAC MP4."""

    if not segment_paths:
        raise LongFormError("没有可合并的已验收分段。", "master_segments_missing")
    try:
        import av
    except ImportError as exc:  # pragma: no cover - bundled Comfy environments include PyAV
        raise LongFormError("当前 Python 缺少 PyAV，无法合并总片。", "pyav_missing") from exc

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    if partial.exists():
        partial.unlink()
    video_pts = 0
    audio_pts = 0
    try:
        with av.open(str(partial), mode="w", format="mp4") as output:
            video_out = output.add_stream("libx264", rate=FPS)
            video_out.width = 1080
            video_out.height = 1920
            video_out.pix_fmt = "yuv420p"
            video_out.options = {"crf": "18", "preset": "medium"}
            audio_out = output.add_stream("aac", rate=48_000)
            audio_out.layout = "stereo"
            audio_out.bit_rate = 192_000

            for source_path in segment_paths:
                with av.open(str(source_path)) as source:
                    if not source.streams.video:
                        raise LongFormError(
                            f"分段没有视频流：{source_path.name}", "media_video_missing"
                        )
                    for frame in source.decode(video=0):
                        frame.pts = video_pts
                        frame.time_base = Fraction(1, FPS)
                        video_pts += 1
                        for packet in video_out.encode(frame):
                            output.mux(packet)
            for packet in video_out.encode():
                output.mux(packet)

            resampler = av.AudioResampler(format="fltp", layout="stereo", rate=48_000)
            for source_path in segment_paths:
                with av.open(str(source_path)) as source:
                    if not source.streams.audio:
                        raise LongFormError(
                            f"分段没有音频流：{source_path.name}", "media_audio_missing"
                        )
                    for frame in source.decode(audio=0):
                        for converted in resampler.resample(frame):
                            converted.pts = audio_pts
                            converted.time_base = Fraction(1, 48_000)
                            audio_pts += int(converted.samples)
                            for packet in audio_out.encode(converted):
                                output.mux(packet)
            for converted in resampler.resample(None):
                converted.pts = audio_pts
                converted.time_base = Fraction(1, 48_000)
                audio_pts += int(converted.samples)
                for packet in audio_out.encode(converted):
                    output.mux(packet)
            for packet in audio_out.encode():
                output.mux(packet)
        if video_pts != int(expected_frames):
            raise LongFormError(
                f"合片前帧数不一致：期望 {expected_frames}，实际 {video_pts}。",
                "master_frame_count_invalid",
            )
        partial.replace(target)
        verified = inspect_media(
            target,
            expected_frames=expected_frames,
            expected_size=(1080, 1920),
            require_audio=True,
        )
    except LongFormError:
        if partial.exists():
            partial.unlink()
        raise
    except Exception as exc:
        if partial.exists():
            partial.unlink()
        raise LongFormError("最终总片合并失败。", "master_assembly_failed") from exc

    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    verified.update(
        {
            "sha256": digest.hexdigest(),
            "codec": "h264",
            "audio_codec": "aac",
            "crf": 18,
            "fps": FPS,
        }
    )
    return verified


class LongFormRuntime:
    def __init__(
        self,
        *,
        store: LongProjectStore,
        project_root: Path,
        provider_call: ProviderCall,
        segment_precompiler: SegmentPrecompiler | None = None,
        comfy_client_factory: Callable[[], ComfyClient] = ComfyClient,
        media_inspector: Callable[..., dict[str, Any]] = inspect_media,
        master_assembler: Callable[..., dict[str, Any]] = assemble_master_video,
        render_lock: threading.Lock | None = None,
    ) -> None:
        self.store = store
        self.project_root = Path(project_root)
        self.assets = ProjectAssetStore(store)
        self.provider_call = provider_call
        self.segment_precompiler = segment_precompiler
        self.comfy_client_factory = comfy_client_factory
        self.media_inspector = media_inspector
        self.master_assembler = master_assembler
        self._requires_media_backend = (
            media_inspector is inspect_media or master_assembler is assemble_master_video
        )
        self._tasks: dict[str, BackgroundTask] = {}
        self._lock = threading.RLock()
        self._render_lock = render_lock or threading.Lock()
        self._precompile_lock = threading.Lock()
        self._reconcile_proposals: dict[str, dict[str, Any]] = {}
        prompts = self.project_root / "prompts"
        vendor = self.project_root / "vendor" / "h3-prompt-writing"
        self.outline_prompt = read_utf8(prompts / "long_outline.md")
        self.segment_prompt = read_utf8(prompts / "long_segment.md")
        self.regenerate_prompt = read_utf8(prompts / "long_regenerate.md")
        self.compiler_prompt = read_utf8(prompts / "long_compiler.md")
        self.qc_prompt = read_utf8(prompts / "long_qc.md")
        self.reconcile_prompt = read_utf8(prompts / "long_reconcile.md")
        self.h3_skill = read_utf8(vendor / "SKILL.md")
        self.h3_base = read_utf8(vendor / "references" / "base-en.txt")
        self.h3_ref = read_utf8(vendor / "references" / "ref-en.txt")

    def _project_picture_path(
        self,
        project_id: str,
        picture: dict[str, Any],
        client: ComfyClient,
    ) -> Path:
        if picture.get("source") == "project_asset":
            return self.assets.resolve(project_id, picture)
        # Backward-compatible one-time migration for projects that were saved
        # when pictures pointed at ComfyUI input. The bytes are fetched through
        # /view and become project-owned before this render is submitted.
        input_name = str(picture.get("input_path") or "").strip()
        if picture.get("source") == "input" and input_name:
            suffix = Path(input_name).suffix.lower()
            mime = MIME_BY_EXTENSION.get(suffix)
            if mime is None:
                raise LongFormError(
                    "旧版 ComfyUI input 图片格式不受支持。", "project_asset_type_invalid"
                )
            migrated = self.assets.save_bytes(
                project_id,
                original_name=Path(input_name).name,
                mime=mime,
                data=client.view_bytes(name=input_name, file_type="input"),
            )
            picture.clear()
            picture.update(migrated)
            return self.assets.resolve(project_id, picture)
        raise LongFormError(
            "参考图尚未保存为项目资产，请重新选择图片并保存本段。",
            "production_picture_missing",
        )

    def _upload_project_picture(
        self,
        project_id: str,
        picture: dict[str, Any],
        client: ComfyClient,
    ) -> dict[str, str]:
        path = self._project_picture_path(project_id, picture, client)
        return client.upload_image(
            path,
            subfolder=f"H3Idea2Video/{project_id}/assets",
            filename=path.name,
        )

    def _upload_handoff_tail(
        self,
        project_id: str,
        path: Path,
        client: ComfyClient,
        *,
        previous_index: int,
    ) -> dict[str, str]:
        if not path.is_file():
            raise LongFormError("上一段项目尾帧不存在。", "tail_missing")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        return client.upload_image(
            path,
            subfolder=f"H3Idea2Video/{project_id}/handoff",
            filename=f"seg_{previous_index:04d}_{digest}.png",
        )

    def task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            item = self._tasks.get(task_id)
            if item is None:
                raise LongFormError("后台任务不存在。", "task_not_found")
            return item.public()

    def tasks_for_project(self, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                item.public()
                for item in self._tasks.values()
                if item.project_id == project_id
            ][-10:]

    def _new_task(self, kind: str, project_id: str) -> BackgroundTask:
        task = BackgroundTask(
            id=kind + "_" + uuid.uuid4().hex[:12], kind=kind, project_id=project_id
        )
        with self._lock:
            self._tasks[task.id] = task
        return task

    @staticmethod
    def _set_task(task: BackgroundTask, **values: Any) -> None:
        for name, value in values.items():
            setattr(task, name, value)
        task.updated_at = utc_now()

    def _provider(
        self,
        messages: list[dict[str, Any]],
        settings: Any,
        task: BackgroundTask,
        *,
        attempts: int = 3,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        for attempt in range(1, attempts + 1):
            try:
                def event(name: str, value: dict[str, Any]) -> None:
                    if name == "delta":
                        task.live_text = (task.live_text + str(value.get("text") or ""))[-6000:]
                        task.updated_at = utc_now()
                    elif name == "thinking":
                        base_message = task.message.split(" · 本地推理", 1)[0]
                        task.message = base_message + " · 本地推理中"
                        task.thinking_text = (
                            task.thinking_text + str(value.get("text") or "")
                        )[-12000:]
                        task.updated_at = utc_now()

                raw, usage, _finish = self.provider_call(
                    messages, settings, event_callback=event
                )
                return raw, usage
            except Exception as exc:
                message, code = _provider_error(exc)
                if code not in RETRYABLE_PROVIDER_CODES or attempt >= attempts:
                    raise LongFormError(message, code) from exc
                self._set_task(
                    task,
                    state="retrying",
                    message=f"LM Studio 本地请求失败，准备第 {attempt + 1}/{attempts} 次尝试…",
                )
                time.sleep((1, 3, 8)[attempt - 1])
        raise AssertionError("unreachable")

    @staticmethod
    def _normalize_reconcile_result(
        raw: Any,
        *,
        segment: dict[str, Any],
        source_state: str,
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise LongFormError(
                "剧情状态同步结果必须是 JSON 对象。",
                "content_sync_response_invalid",
            )
        value = raw.get("segment") if isinstance(raw.get("segment"), dict) else raw
        title = str(value.get("title") or "").strip()
        ending_state = str(value.get("ending_state") or "").strip()
        story_text = str(value.get("story_text") or "").strip()
        characters = value.get("present_characters")
        if not title or not ending_state or not isinstance(characters, list):
            raise LongFormError(
                "剧情状态同步缺少 title、ending_state 或 present_characters。",
                "content_sync_response_invalid",
            )
        if source_state == "shots_dirty" and not story_text:
            raise LongFormError(
                "Shot 反向同步时必须返回完整 story_text。",
                "content_sync_response_invalid",
            )
        index = int(segment.get("index") or 0)
        recommended = str(value.get("recommended_boundary_before") or "").lower()
        if index == 1:
            recommended = "start"
        elif recommended not in {"continuous", "cut"}:
            raise LongFormError(
                "剧情状态同步返回了无效边界建议。",
                "content_sync_response_invalid",
            )
        compatible = value.get("continuity_compatible")
        if not isinstance(compatible, bool):
            raise LongFormError(
                "剧情状态同步缺少 continuity_compatible 布尔值。",
                "content_sync_response_invalid",
            )
        warnings = value.get("warnings") or []
        if not isinstance(warnings, list):
            warnings = [str(warnings)]
        return {
            "title": title[:500],
            "story_text": story_text,
            "ending_state": ending_state[:20_000],
            "present_characters": list(
                dict.fromkeys(str(item).strip() for item in characters if str(item).strip())
            ),
            "recommended_boundary_before": recommended,
            "boundary_reason": str(value.get("boundary_reason") or "").strip()[:4_000],
            "continuity_compatible": compatible,
            "conflict_message": str(value.get("conflict_message") or "").strip()[:4_000],
            "warnings": [str(item).strip() for item in warnings if str(item).strip()],
        }

    def _commit_reconciliation(
        self,
        candidate: dict[str, Any],
        *,
        accept_boundary: bool,
    ) -> dict[str, Any]:
        project_id = str(candidate["project_id"])
        segment_id = str(candidate["segment_id"])
        with self.store.transaction():
            project = self.store.load(project_id)
            segment = next(
                (item for item in project["segments"] if item["id"] == segment_id),
                None,
            )
            if segment is None:
                raise LongFormError("分段不存在。", "segment_not_found")
            workspace = segment.get("single_workspace") or {}
            if (
                int(project.get("current_revision") or 0)
                != int(candidate["project_revision"])
                or int(workspace.get("revision") or 0)
                != int(candidate["workspace_revision"])
                or story_source_hash(segment) != candidate["story_hash"]
                or shot_source_hash(segment) != candidate["shots_hash"]
            ):
                raise LongFormError(
                    "剧情或 Shot 已在同步期间变化，请重新点击同步。",
                    "content_sync_conflict",
                )

            current_boundary = str(segment.get("boundary_before") or "start")
            recommended = str(candidate["recommended_boundary_before"])
            boundary = recommended if accept_boundary else current_boundary
            if (
                boundary == current_boundary
                and not bool(candidate["continuity_compatible"])
            ):
                raise LongFormError(
                    candidate.get("conflict_message")
                    or "当前开场状态无法承接新的剧情内容；请接受切镜建议或修改 Shot。",
                    "content_sync_continuity_conflict",
                )

            self.store.snapshot(project, reason=f"同步 {segment_id} 剧情状态前")
            card = segment["story_card"]
            fixed_opening = str(card.get("opening_state") or "")
            source_state = str(candidate["source_state"])
            card["title"] = str(candidate["title"])
            if source_state == "shots_dirty":
                card["story_text"] = str(candidate["story_text"])
            card["ending_state"] = str(candidate["ending_state"])
            card["present_characters"] = list(candidate["present_characters"])
            card["dialogue"] = aggregate_shot_dialogue(segment)
            card["opening_state"] = fixed_opening
            card["boundary_before"] = boundary

            segment["boundary_before"] = boundary
            segment["summary"] = card["story_text"]
            segment["dialogue"] = copy.deepcopy(card["dialogue"])
            segment["continuity_in"] = fixed_opening
            segment["continuity_out"] = card["ending_state"]
            segment["present_characters"] = list(card["present_characters"])
            segment["h3_prompt"] = ""
            segment["prompt_state"] = "stale"
            segment["prompt_warnings"] = []
            segment["render_state"] = (
                "stale" if segment.get("artifacts") else "pending"
            )
            if source_state == "story_dirty":
                segment["script_state"] = (
                    "possibly_stale"
                    if isinstance(workspace.get("script"), dict)
                    else "planned"
                )
            else:
                segment["script_state"] = "ready"
                workspace["preserve_script_on_precompile"] = True

            form = workspace.setdefault("form", {})
            form["creative_brief"] = card["story_text"]
            form["action_timeline"] = card["story_text"]
            form["exact_dialogue"] = "\n".join(
                item["text"] for item in card["dialogue"]
            )
            form["duration"] = int(segment["frames"]) / FPS
            pictures = workspace.setdefault("pictures", {})
            picture1 = pictures.setdefault(
                "picture1",
                {"source": "none", "input_path": "", "temporary_name": ""},
            )
            if segment["index"] > 1 and boundary == "continuous":
                form["mode"] = (
                    form.get("mode") if form.get("mode") in {"I2VA", "FL2VA"} else "I2VA"
                )
                form["picture1_description"] = fixed_opening
                picture1.update(
                    {"source": "auto_tail", "input_path": "", "temporary_name": ""}
                )
            elif picture1.get("source") == "auto_tail":
                picture1.update(
                    {"source": "none", "input_path": "", "temporary_name": ""}
                )
                form["mode"] = "T2VA"
                form["picture1_description"] = ""
            workspace["prompt"] = ""
            workspace["state"] = "stale"
            workspace["validation"] = {
                "valid": False,
                "errors": ["剧情状态已同步；必须重新编译当前段 H3 提示词。"],
                "warnings": list(candidate.get("warnings") or []),
            }
            workspace["warnings"] = list(candidate.get("warnings") or [])
            workspace["revision"] = int(workspace.get("revision") or 0) + 1
            workspace["updated_at"] = utc_now()
            segment["content_sync"] = make_content_sync(
                segment,
                state="clean",
                source="explicit_reconciliation",
            )

            for downstream in project["segments"][int(segment["index"]) :]:
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
                    source=f"upstream_reconcile_{segment['index']}",
                )
            project["status"] = "stale"
            project["stale_from"] = min(
                int(project.get("stale_from") or segment["index"]),
                int(segment["index"]),
            )
            project["master"] = {}
            project.setdefault("scheduler", {})["last_error"] = None
            merge_usage(project.setdefault("usage", {}), candidate.get("usage") or {})
            return self.store.save(project)

    def start_reconcile(
        self,
        project_id: str,
        segment_id: str,
        settings: Any,
    ) -> dict[str, Any]:
        project = self.store.load(project_id)
        segment = next(
            (item for item in project["segments"] if item["id"] == segment_id),
            None,
        )
        if segment is None:
            raise LongFormError("分段不存在。", "segment_not_found")
        source_state = str((segment.get("content_sync") or {}).get("state") or "")
        if source_state not in {"story_dirty", "shots_dirty", "failed"}:
            raise LongFormError(
                "本段剧情状态没有变化，无需同步。", "content_sync_not_required"
            )
        if source_state == "failed":
            source_state = str(
                (segment.get("content_sync") or {}).get("source_state")
                or "story_dirty"
            )
        workspace = segment.get("single_workspace") or {}
        if source_state == "shots_dirty" and not isinstance(workspace.get("script"), dict):
            raise LongFormError(
                "Shot 同步需要已保存的分镜 JSON。", "content_sync_shots_missing"
            )
        expected = {
            "project_revision": int(project.get("current_revision") or 0),
            "workspace_revision": int(workspace.get("revision") or 0),
            "story_hash": story_source_hash(segment),
            "shots_hash": shot_source_hash(segment),
        }
        task = self._new_task("reconcile", project_id)

        def worker() -> None:
            try:
                self._set_task(
                    task,
                    state="running",
                    stage="content_reconcile",
                    message=f"同步第 {segment['index']} 段剧情正文、Shot 与结束状态…",
                    total=1,
                    current=0,
                )
                previous = (
                    project["segments"][int(segment["index"]) - 2]
                    if int(segment["index"]) > 1
                    else None
                )
                exact_dialogue = aggregate_shot_dialogue(segment)
                context = {
                    "task": "reconcile_story_and_saved_shots",
                    "source_of_change": source_state,
                    "segment_index": segment["index"],
                    "segment_count": len(project["segments"]),
                    "duration_seconds": int(segment["frames"]) / FPS,
                    "fixed_opening_state": (segment.get("story_card") or {}).get("opening_state") or "",
                    "current_boundary_before": segment.get("boundary_before"),
                    "previous_segment_ending_state": (
                        (previous.get("story_card") or {}).get("ending_state")
                        if previous
                        else ""
                    ),
                    "story_bible": project.get("story_bible") or {},
                    "outline": project.get("outline") or [],
                    "story_target": segment.get("story_target") or {},
                    "current_story_card": segment.get("story_card") or {},
                    "saved_shot_script": workspace.get("script"),
                    "exact_dialogue_read_only": exact_dialogue,
                    "rules": [
                        "Never rewrite fixed_opening_state.",
                        "Do not return or edit dialogue; the program copies it from Shot deterministically.",
                        "If source_of_change is story_dirty, current_story_card.story_text is authoritative.",
                        "If source_of_change is shots_dirty, saved_shot_script is authoritative and you must rewrite story_text to match it.",
                        "Evaluate whether the fixed opening can reach the first Shot. Suggest cut only when continuous is no longer coherent.",
                    ],
                }
                raw, usage = self._provider(
                    [
                        {"role": "system", "content": self.reconcile_prompt},
                        {"role": "user", "content": _json_user(context)},
                    ],
                    settings,
                    task,
                    attempts=2,
                )
                normalized = self._normalize_reconcile_result(
                    raw,
                    segment=segment,
                    source_state=source_state,
                )
                candidate = {
                    "project_id": project_id,
                    "segment_id": segment_id,
                    "source_state": source_state,
                    **expected,
                    **normalized,
                    "usage": usage,
                }
                current_boundary = str(segment.get("boundary_before") or "start")
                if normalized["recommended_boundary_before"] != current_boundary:
                    proposal_id = "reconcile_" + uuid.uuid4().hex
                    with self._lock:
                        self._reconcile_proposals[proposal_id] = candidate
                    task.result = {
                        "requires_boundary_confirmation": True,
                        "proposal_id": proposal_id,
                        "segment_id": segment_id,
                        "current_boundary_before": current_boundary,
                        "recommended_boundary_before": normalized[
                            "recommended_boundary_before"
                        ],
                        "boundary_reason": normalized["boundary_reason"],
                    }
                    self._set_task(
                        task,
                        state="completed",
                        stage="awaiting_boundary_confirmation",
                        current=1,
                        message="剧情状态已分析；需要确认边界建议后才会保存。",
                        live_text="",
                    )
                else:
                    updated = self._commit_reconciliation(
                        candidate,
                        accept_boundary=False,
                    )
                    task.result = {
                        "reconciled": True,
                        "segment_id": segment_id,
                        "boundary_before": current_boundary,
                    }
                    self._set_task(
                        task,
                        state="completed",
                        stage="done",
                        current=1,
                        message="本段剧情状态已同步；当前 H3 提示词已标记为必须重新编译。",
                        live_text="",
                    )
            except Exception as exc:
                message, code = _provider_error(exc)
                try:
                    with self.store.transaction():
                        failed_project = self.store.load(project_id)
                        failed_segment = next(
                            item
                            for item in failed_project["segments"]
                            if item["id"] == segment_id
                        )
                        if (
                            story_source_hash(failed_segment) == expected["story_hash"]
                            and shot_source_hash(failed_segment) == expected["shots_hash"]
                        ):
                            failed_segment["content_sync"] = make_content_sync(
                                failed_segment,
                                state="failed",
                                source="explicit_reconciliation",
                                error=message,
                            )
                            failed_segment["content_sync"]["source_state"] = source_state
                            self.store.save(failed_project)
                except Exception:
                    pass
                self._set_task(
                    task,
                    state="failed",
                    stage="failed",
                    message=message,
                    error={"code": code, "message": message},
                )
            finally:
                task.finished_at = utc_now()
                task.updated_at = task.finished_at

        threading.Thread(
            target=worker,
            name=f"h3-reconcile-{project_id}-{segment_id}",
            daemon=True,
        ).start()
        return {"task": task.public(), "project_id": project_id}

    def commit_reconcile_proposal(
        self,
        proposal_id: str,
        *,
        accept_boundary: bool,
    ) -> dict[str, Any]:
        with self._lock:
            candidate = self._reconcile_proposals.pop(str(proposal_id), None)
        if candidate is None:
            raise LongFormError(
                "边界确认已过期，请重新点击同步。", "content_sync_proposal_not_found"
            )
        updated = self._commit_reconciliation(
            candidate,
            accept_boundary=bool(accept_boundary),
        )
        return updated

    def _validated_segment_with_repairs(
        self,
        *,
        system_prompt: str,
        context: dict[str, Any],
        settings: Any,
        task: BackgroundTask,
        usage_target: dict[str, Any],
        segment_index: int,
        frames: int,
        previous_state: str,
    ) -> dict[str, Any]:
        """Generate one segment, repairing schema/timeline failures at most twice."""

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _json_user(context)},
        ]
        audits: list[dict[str, Any]] = []
        for semantic_attempt in range(1, 4):
            task.live_text = ""
            raw, usage = self._provider(messages, settings, task)
            merge_usage(usage_target, usage)
            audit: dict[str, Any] = {
                "attempt": semantic_attempt,
                "created_at": utc_now(),
                "response": copy.deepcopy(raw),
                "usage": copy.deepcopy(usage),
                "state": "invalid",
                "validation_error": None,
            }
            if isinstance(raw.get("conflict"), dict):
                conflict = raw["conflict"]
                audit["validation_error"] = {
                    "code": "regeneration_anchor_conflict",
                    "message": str(
                        conflict.get("message") or "保留锚点与新剧情冲突。"
                    ),
                }
                audits.append(audit)
                error = LongFormError(
                    audit["validation_error"]["message"],
                    "regeneration_anchor_conflict",
                )
                error.attempts = audits  # type: ignore[attr-defined]
                raise error
            try:
                segment = normalize_segment_result(
                    raw,
                    index=segment_index,
                    frames=frames,
                    previous_state=previous_state,
                    story_target=context.get("story_target"),
                )
                dialogue_texts = _dialogue_texts([segment])
                already_fixed = {
                    str(item)
                    for item in (
                        context.get("exact_dialogue_already_used")
                        or context.get("exact_dialogue_already_fixed")
                        or []
                    )
                }
                repeated = sorted(dialogue_texts & already_fixed)
                if repeated:
                    raise LongFormError(
                        "本段重复了已经使用的指定对白：" + "；".join(repeated[:5]),
                        "long_dialogue_duplicated",
                    )
                if int(context.get("segment_index") or segment_index) == int(
                    context.get("segment_count") or segment_index
                ):
                    remaining = {
                        str(item)
                        for item in context.get("exact_dialogue_remaining") or []
                    }
                    missing = sorted(remaining - dialogue_texts)
                    if missing:
                        raise LongFormError(
                            "最后一段缺少必须原样保留的对白：" + "；".join(missing[:5]),
                            "long_dialogue_not_preserved",
                        )
            except LongFormError as exc:
                audit["validation_error"] = {
                    "code": exc.code,
                    "message": exc.message,
                }
                audits.append(audit)
                if semantic_attempt >= 3:
                    raise SegmentRepairExhausted(segment_index, audits) from exc
                self._set_task(
                    task,
                    state="retrying",
                    message=(
                        f"第 {segment_index} 段时间轴无效，自动修复 "
                        f"{semantic_attempt}/2：{exc.message}"
                    ),
                )
                messages.extend(
                    [
                        {
                            "role": "assistant",
                            "content": json.dumps(raw, ensure_ascii=False),
                        },
                        {
                            "role": "user",
                            "content": _json_user(
                                {
                                    "task": "repair_invalid_segment_json",
                                    "validation_error": audit["validation_error"],
                                    "authoritative_timing": {
                                        "fps": FPS,
                                        "frames": int(frames),
                                        "duration_seconds": int(frames) / FPS,
                                    },
                                    "requirements": [
                                        "Return the complete segment JSON again, not a patch.",
                                        "Use beats as a non-empty array; every beat action is a string.",
                                        "Beats must cover the complete segment with no gap, overlap, or out-of-range endpoint.",
                                        "Preserve exact dialogue, visible text, story facts, and continuity unchanged.",
                                        "Fulfill the supplied story_target, return its exact chapter_numbers, and copy all required_ending_conditions exactly.",
                                        "When must_close_story is true, end the whole story without an unresolved next objective.",
                                    ],
                                }
                            ),
                        },
                    ]
                )
                continue
            audit["state"] = "accepted"
            audits.append(audit)
            segment["script_attempts"] = audits
            self._set_task(task, state="running")
            return segment
        raise AssertionError("unreachable")

    def _validated_story_card_with_repairs(
        self,
        *,
        system_prompt: str,
        context: dict[str, Any],
        settings: Any,
        task: BackgroundTask,
        usage_target: dict[str, Any],
        segment_index: int,
        previous_state: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Generate a concise story card, with at most two semantic repairs."""

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _json_user(context)},
        ]
        audits: list[dict[str, Any]] = []
        for semantic_attempt in range(1, 4):
            task.live_text = ""
            raw, usage = self._provider(messages, settings, task)
            merge_usage(usage_target, usage)
            audit: dict[str, Any] = {
                "attempt": semantic_attempt,
                "created_at": utc_now(),
                "response": copy.deepcopy(raw),
                "usage": copy.deepcopy(usage),
                "state": "invalid",
                "validation_error": None,
            }
            try:
                card = normalize_story_card_result(
                    raw,
                    index=segment_index,
                    story_target=context["story_target"],
                    previous_state=previous_state,
                )
                dialogue_texts = _dialogue_texts([card])
                already_used = {
                    str(item)
                    for item in (
                        context.get("exact_dialogue_already_used")
                        or context.get("exact_dialogue_already_fixed")
                        or []
                    )
                }
                repeated = sorted(dialogue_texts & already_used)
                if repeated:
                    raise LongFormError(
                        "本段重复了已经使用的指定对白：" + "；".join(repeated[:5]),
                        "long_dialogue_duplicated",
                    )
                if int(context.get("segment_index") or segment_index) == int(
                    context.get("segment_count") or segment_index
                ):
                    remaining = {
                        str(item)
                        for item in context.get("exact_dialogue_remaining") or []
                    }
                    missing = sorted(remaining - dialogue_texts)
                    if missing:
                        raise LongFormError(
                            "最后一段缺少必须原样保留的对白：" + "；".join(missing[:5]),
                            "long_dialogue_not_preserved",
                        )
            except LongFormError as exc:
                audit["validation_error"] = {"code": exc.code, "message": exc.message}
                audits.append(audit)
                if semantic_attempt >= 3:
                    raise SegmentRepairExhausted(segment_index, audits) from exc
                self._set_task(
                    task,
                    state="retrying",
                    message=(
                        f"第 {segment_index} 段剧情卡片无效，自动修复 "
                        f"{semantic_attempt}/2：{exc.message}"
                    ),
                )
                messages.extend(
                    [
                        {"role": "assistant", "content": json.dumps(raw, ensure_ascii=False)},
                        {
                            "role": "user",
                            "content": _json_user(
                                {
                                    "task": "repair_story_card_json",
                                    "validation_error": audit["validation_error"],
                                    "requirements": [
                                        "Return the complete story-card JSON, not a patch.",
                                        "Do not add shots, beats, timestamps, camera, sound, or H3 fields.",
                                        "Preserve exact dialogue byte-for-byte and use every assigned line once.",
                                        "Return the exact story_target chapter_numbers and ending requirements.",
                                    ],
                                }
                            ),
                        },
                    ]
                )
                continue
            audit["state"] = "accepted"
            audits.append(audit)
            self._set_task(task, state="running")
            return card, audits
        raise AssertionError("unreachable")

    def start_new_project(self, payload: dict[str, Any], settings: Any) -> dict[str, Any]:
        idea = str(payload.get("idea") or "").strip()
        if not idea:
            raise LongFormError("请填写长视频 Idea。", "idea_required")
        project_id = (
            time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        )
        task = self._new_task("script", project_id)

        def worker() -> None:
            try:
                self._set_task(task, state="running", stage="outline", message="生成故事圣经与全局大纲…")
                request_context = {
                    "idea": idea,
                    "target_seconds": payload.get("target_seconds"),
                    "visual_style": payload.get("visual_style") or "",
                    "characters": payload.get("characters") or "",
                    "exact_dialogue": payload.get("exact_dialogue") or "",
                    "music": payload.get("music") or "",
                    "constraints": payload.get("constraints") or "",
                }
                if payload.get("initial_frame") or payload.get("identity_references"):
                    raise LongFormError(
                        "长项目参考图请在剧情生成后绑定到具体分段，不能再引用 ComfyUI input 路径。",
                        "long_reference_api_removed",
                    )
                reference_assets: list[dict[str, str]] = []
                request_context["reference_assets"] = reference_assets
                user_content: str | list[dict[str, Any]] = _json_user(request_context)
                raw, usage = self._provider(
                    [
                        {"role": "system", "content": self.outline_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    settings,
                    task,
                )
                outline = normalize_outline_result(raw, idea=idea)
                returned_references = {
                    str(item.get("id") or ""): item
                    for item in outline.get("reference_analysis") or []
                }
                missing_references = [
                    item["id"] for item in reference_assets if item["id"] not in returned_references
                ]
                if missing_references:
                    raise LongFormError(
                        "参考图分析缺少：" + "、".join(missing_references),
                        "long_reference_analysis_missing",
                    )
                extra_references = sorted(
                    set(returned_references) - {item["id"] for item in reference_assets}
                )
                if extra_references:
                    raise LongFormError(
                        "参考图分析包含未提供的图片：" + "、".join(extra_references),
                        "long_reference_analysis_extra",
                    )
                mismatched_references = [
                    item["id"]
                    for item in reference_assets
                    if str(returned_references[item["id"]].get("source_name") or "")
                    != item["source_name"]
                ]
                if mismatched_references:
                    raise LongFormError(
                        "参考图分析改变了来源标识：" + "、".join(mismatched_references),
                        "long_reference_analysis_mismatch",
                    )
                requested = payload.get("target_seconds")
                seconds = float(requested) if requested not in (None, "") else outline["suggested_total_seconds"]
                plan = allocate_segment_frames(seconds)
                project = make_project(outline, plan, project_id=project_id)
                project["creative_options"] = request_context
                project["reference_assets"] = reference_assets
                project["exact_dialogue_required"] = exact_dialogue_lines(
                    payload.get("exact_dialogue")
                )
                project["initial_frame"] = payload.get("initial_frame") or None
                project["user_identity_references"] = list(payload.get("identity_references") or [])
                merge_usage(project["usage"], usage)
                self.store.save(project)
                task.total = len(project["segments"])

                previous_state = "The project has not started yet."
                prior_summaries: list[str] = []
                used_exact_dialogue: set[str] = set()
                for index, placeholder in enumerate(project["segments"], start=1):
                    if task.stop_requested:
                        project["status"] = "paused"
                        self.store.save(project)
                        self._set_task(task, state="paused", message="已在下一段生成前暂停。")
                        return
                    self._set_task(
                        task,
                        state="running",
                        stage="segments",
                        current=index,
                        message=f"生成第 {index}/{len(project['segments'])} 段剧本…",
                        live_text="",
                    )
                    context = {
                        "story_bible": project["story_bible"],
                        "outline": project["outline"],
                        "ending_requirements": project["ending_requirements"],
                        "segment_index": index,
                        "segment_count": len(project["segments"]),
                        "frames": placeholder["frames"],
                        "duration_seconds": placeholder["frames"] / FPS,
                        "story_target": placeholder["story_target"],
                        "previous_state": previous_state,
                        "recent_summaries": prior_summaries[-8:],
                        "creative_options": project["creative_options"],
                        "exact_dialogue_already_used": sorted(used_exact_dialogue),
                        "exact_dialogue_remaining": [
                            line
                            for line in project["exact_dialogue_required"]
                            if line not in used_exact_dialogue
                        ],
                    }
                    try:
                        card, audits = self._validated_story_card_with_repairs(
                            system_prompt=self.segment_prompt,
                            context=context,
                            settings=settings,
                            task=task,
                            usage_target=project["usage"],
                            segment_index=index,
                            previous_state=previous_state,
                        )
                    except SegmentRepairExhausted as exc:
                        placeholder["script_attempts"] = exc.attempts
                        placeholder["script_state"] = "failed"
                        placeholder["timeline_state"] = "invalid"
                        project.setdefault("script_attempt_log", []).append(
                            {
                                "phase": "initial",
                                "segment_index": index,
                                "segment_id": placeholder["id"],
                                "attempts": copy.deepcopy(exc.attempts),
                            }
                        )
                        self.store.save(project)
                        raise
                    segment = apply_story_card(
                        placeholder,
                        card,
                        creative_options=project["creative_options"],
                    )
                    segment["script_attempts"] = audits
                    project["segments"][index - 1] = segment
                    project.setdefault("script_attempt_log", []).append(
                        {
                            "phase": "initial",
                            "segment_index": index,
                            "segment_id": segment["id"],
                            "attempts": copy.deepcopy(segment["script_attempts"]),
                        }
                    )
                    previous_state = card["ending_state"]
                    prior_summaries.append(card["story_text"])
                    used_exact_dialogue.update(_dialogue_texts([segment]))
                    self.store.save(project)
                ensure_required_dialogue(project)
                project["status"] = "draft"
                project["stale_from"] = None
                self.store.snapshot(project, reason="长剧本首次生成完成")
                self.store.save(project)
                self._set_task(
                    task,
                    state="completed",
                    stage="done",
                    message="多段剧情卡片已生成；请选择段落继续使用单段工作台。",
                )
            except Exception as exc:
                message, code = _provider_error(exc)
                error = {"code": code, "message": message}
                try:
                    project = self.store.load(project_id)
                    project["status"] = "failed"
                    project["scheduler"]["last_error"] = error
                    self.store.save(project)
                except LongFormError:
                    pass
                self._set_task(
                    task,
                    state="failed",
                    error=error,
                    message=message,
                )
            finally:
                task.finished_at = utc_now()
                task.updated_at = task.finished_at

        threading.Thread(target=worker, name=f"h3-long-script-{project_id}", daemon=True).start()
        return {"task": task.public(), "project_id": project_id}

    def start_regeneration(
        self,
        project_id: str,
        payload: dict[str, Any],
        settings: Any,
    ) -> dict[str, Any]:
        source = self.store.load(project_id)
        edited_index = int(payload.get("edited_index") or 0)
        plan_info = build_regeneration_plan(
            source,
            edited_index=edited_index,
            keep_segment_ids=payload.get("keep_segment_ids") or [],
            duration_policy=str(payload.get("duration_policy") or "fixed"),
        )
        task = self._new_task("regenerate", project_id)

        def worker() -> None:
            try:
                self._set_task(task, state="running", stage="regenerate", message="准备下游重生成…")
                project = self.store.load(project_id)
                self.store.snapshot(project, reason=f"从第 {edited_index + 1} 段重生成前")
                candidate = copy.deepcopy(project)
                keep_ids = set(plan_info["anchor_ids"])

                if plan_info["duration_policy"] == "replan":
                    requested_count = int(payload.get("new_segment_count") or len(candidate["segments"]))
                    if requested_count <= edited_index:
                        raise LongFormError("新段数必须大于已编辑段编号。", "segment_count_invalid")
                    prefix = copy.deepcopy(candidate["segments"][:edited_index])
                    old_tail = candidate["segments"][edited_index:]
                    anchors = [copy.deepcopy(item) for item in old_tail if item["id"] in keep_ids]
                    for anchor in anchors:
                        anchor["locked"] = True
                    slots: list[dict[str, Any] | None] = prefix + [None] * (requested_count - edited_index)
                    last_position = edited_index
                    old_span = max(1, len(candidate["segments"]) - edited_index)
                    for anchor_number, anchor in enumerate(anchors):
                        remaining_anchors = len(anchors) - anchor_number - 1
                        relative = (anchor["index"] - edited_index) / old_span
                        proposed = edited_index + max(1, round(relative * (requested_count - edited_index)))
                        position = max(last_position + 1, proposed)
                        position = min(position, requested_count - remaining_anchors)
                        slots[position - 1] = anchor
                        last_position = position
                    for index in range(edited_index, requested_count):
                        if slots[index] is None:
                            slots[index] = copy.deepcopy(old_tail[min(index - edited_index, len(old_tail) - 1)])
                            slots[index]["provenance"] = "placeholder"
                            slots[index]["script_state"] = "pending"
                            slots[index]["locked"] = False
                    preserve_positions = set(range(edited_index)) | {
                        position
                        for position, item in enumerate(slots)
                        if item is not None and item.get("locked")
                    }
                    candidate["segments"] = reindex_and_allocate(
                        [item for item in slots if item is not None],
                        total_frames=candidate["target_frames"],
                        preserve_positions=preserve_positions,
                    )
                    baseline_targets = build_segment_story_targets(
                        candidate["outline"],
                        candidate["ending_requirements"],
                        len(candidate["segments"]),
                    )
                    for target_position, segment_item in enumerate(candidate["segments"]):
                        baseline_target = baseline_targets[target_position]
                        if target_position not in preserve_positions:
                            segment_item["story_target"] = baseline_target
                            continue
                        old_target = copy.deepcopy(segment_item.get("story_target") or {})
                        if old_target.get("must_close_story") and target_position != len(candidate["segments"]) - 1:
                            raise LongFormError(
                                "保留的结尾锚点不能被移动到新时间线中段。",
                                "regeneration_anchor_conflict",
                            )
                        old_target.update(
                            {
                                "segment_index": target_position + 1,
                                "segment_count": len(candidate["segments"]),
                                "story_progress_start": baseline_target["story_progress_start"],
                                "story_progress_end": baseline_target["story_progress_end"],
                                "required_ending_conditions": baseline_target[
                                    "required_ending_conditions"
                                ],
                                "must_close_story": baseline_target["must_close_story"],
                            }
                        )
                        segment_item["story_target"] = old_target
                    keep_ids = {
                        item["id"]
                        for item in candidate["segments"][edited_index:]
                        if item.get("locked")
                    }
                    for item in candidate["segments"][max(0, edited_index - 1) :]:
                        item["prompt_state"] = "stale"
                        item["render_state"] = "stale"
                    candidate["stale_from"] = min(
                        int(candidate.get("stale_from") or edited_index), edited_index
                    )

                total_actions = len(candidate["segments"]) - edited_index
                task.total = total_actions
                previous_state = candidate["segments"][edited_index - 1]["story_card"]["ending_state"]
                fixed_dialogue_segments = candidate["segments"][:edited_index] + [
                    item
                    for item in candidate["segments"][edited_index:]
                    if item["id"] in keep_ids
                ]
                used_exact_dialogue = _dialogue_texts(fixed_dialogue_segments)
                for position in range(edited_index, len(candidate["segments"])):
                    item = candidate["segments"][position]
                    task.current = position - edited_index + 1
                    if item["id"] in keep_ids:
                        item["script_state"] = "planned"
                        item["timeline_state"] = "invalid"
                        item["beats"] = []
                        item["prompt_state"] = "stale"
                        item["h3_prompt"] = ""
                        workspace = item.get("single_workspace")
                        if isinstance(workspace, dict):
                            workspace["state"] = "stale"
                            workspace["prompt"] = ""
                            workspace["validation"] = {
                                "valid": False,
                                "errors": [
                                    "上游剧情已改变；保留剧情锚点后仍需重新生成单段分镜。"
                                ],
                                "warnings": [],
                            }
                        item["locked"] = True
                        previous_state = item["story_card"]["ending_state"]
                        continue
                    future_anchor = next(
                        (
                            candidate["segments"][later]
                            for later in range(position + 1, len(candidate["segments"]))
                            if candidate["segments"][later]["id"] in keep_ids
                        ),
                        None,
                    )
                    self._set_task(
                        task,
                        state="running",
                        current=task.current,
                        message=f"重写第 {position + 1} 段…",
                        live_text="",
                    )
                    context = {
                        "edited_segment": candidate["segments"][edited_index - 1],
                        "story_bible": candidate["story_bible"],
                        "outline": candidate["outline"],
                        "ending_requirements": candidate["ending_requirements"],
                        "required_exact_dialogue": candidate.get("exact_dialogue_required") or [],
                        "exact_dialogue_already_fixed": sorted(used_exact_dialogue),
                        "exact_dialogue_remaining": [
                            line
                            for line in candidate.get("exact_dialogue_required") or []
                            if line not in used_exact_dialogue
                        ],
                        "segment_index": position + 1,
                        "segment_count": len(candidate["segments"]),
                        "frames": item["frames"],
                        "duration_seconds": item["frames"] / FPS,
                        "story_target": item["story_target"],
                        "previous_state": previous_state,
                        "future_anchor": future_anchor,
                    }
                    try:
                        card, audits = self._validated_story_card_with_repairs(
                            system_prompt=(
                                self.segment_prompt
                                + "\n\n# Downstream regeneration rules\n"
                                + self.regenerate_prompt
                            ),
                            context=context,
                            settings=settings,
                            task=task,
                            usage_target=candidate["usage"],
                            segment_index=position + 1,
                            previous_state=previous_state,
                        )
                    except LongFormError as exc:
                        attempts = copy.deepcopy(getattr(exc, "attempts", []))
                        if attempts:
                            current = self.store.load(project_id)
                            current.setdefault("script_attempt_log", []).append(
                                {
                                    "phase": "regeneration_failed",
                                    "segment_index": position + 1,
                                    "segment_id": item["id"],
                                    "attempts": attempts,
                                }
                            )
                            for audit in attempts:
                                merge_usage(current["usage"], audit.get("usage") or {})
                            self.store.save(current)
                        raise
                    replacement = apply_story_card(
                        item,
                        card,
                        creative_options=candidate.get("creative_options") or {},
                    )
                    replacement["script_attempts"] = audits
                    candidate["segments"][position] = replacement
                    candidate.setdefault("script_attempt_log", []).append(
                        {
                            "phase": "regeneration",
                            "segment_index": position + 1,
                            "segment_id": replacement["id"],
                            "attempts": copy.deepcopy(replacement["script_attempts"]),
                        }
                    )
                    previous_state = card["ending_state"]
                    used_exact_dialogue.update(_dialogue_texts([replacement]))
                ensure_required_dialogue(candidate)
                candidate["status"] = "stale"
                candidate["master"] = {}
                candidate["stale_from"] = min(
                    int(candidate.get("stale_from") or edited_index), edited_index
                )
                validate_project(candidate)
                self.store.save(candidate)
                self._set_task(task, state="completed", stage="done", message="后续剧本已重生成。")
            except Exception as exc:
                message, code = _provider_error(exc)
                self._set_task(
                    task,
                    state="failed",
                    message=message,
                    error={"code": code, "message": message},
                )
            finally:
                task.finished_at = utc_now()
                task.updated_at = task.finished_at

        threading.Thread(target=worker, name=f"h3-regenerate-{project_id}", daemon=True).start()
        return {"task": task.public(), "plan": plan_info}

    def _identity_refs_for_segment(self, project: dict[str, Any], segment: dict[str, Any]) -> list[dict[str, str]]:
        descriptions = {
            str(item.get("source_name") or ""): str(item.get("description") or "")
            for item in project.get("reference_analysis") or []
            if isinstance(item, dict)
        }
        result: list[dict[str, str]] = []
        configured = project.get("user_identity_references") or []
        for item in configured:
            if isinstance(item, str) and item:
                result.append(
                    {
                        "type": "input",
                        "name": item,
                        "character": "",
                        "description": descriptions.get(item, ""),
                    }
                )
            elif isinstance(item, dict) and item.get("name"):
                result.append(
                    {
                        "type": str(item.get("type") or "input"),
                        "name": str(item["name"]),
                        "character": str(item.get("character") or ""),
                        "description": str(item.get("description") or descriptions.get(str(item["name"]), "")),
                    }
                )
        auto = project.get("identity_references") or {}
        present = set(segment.get("present_characters") or [])
        for character, item in auto.items():
            if character not in present or not isinstance(item, dict) or not item.get("name"):
                continue
            result.append(
                {
                    "type": str(item.get("type") or "output"),
                    "name": str(item["name"]),
                    "character": character,
                    "description": str(
                        item.get("description")
                        or f"Accepted native identity sample of {character} from segment {item.get('source_segment')}."
                    ),
                }
            )
        unique: list[dict[str, str]] = []
        seen = set()
        for item in result:
            key = (item["type"], item["name"])
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    def _compile_segment(
        self,
        project: dict[str, Any],
        segment: dict[str, Any],
        settings: Any,
        task: BackgroundTask,
        *,
        first_frame: dict[str, str] | None,
        refs: list[dict[str, str]],
        correction: str = "",
    ) -> dict[str, Any]:
        guide = self.h3_ref if refs else self.h3_base
        context = {
            "segment": segment,
            "frame_count": segment["frames"],
            "duration_seconds": segment["frames"] / FPS,
            "has_first_frame": bool(first_frame),
            "first_frame": first_frame,
            "fixed_reference_images": refs,
            "tail_picture_ordinal": len(refs) + 1 if first_frame and refs else None,
            "prompt_correction": correction,
        }
        raw, usage = self._provider(
            [
                {
                    "role": "system",
                    "content": self.compiler_prompt + "\n\nOFFICIAL SKILL:\n" + self.h3_skill + "\n\nOFFICIAL GUIDE:\n" + guide,
                },
                {"role": "user", "content": _json_user(context)},
            ],
            settings,
            task,
        )
        compiled = normalize_compile_result(
            raw,
            segment=segment,
            has_first_frame=bool(first_frame),
            reference_count=len(refs),
        )
        merge_usage(project["usage"], usage)
        return compiled

    def start_precompile(self, project_id: str, settings: Any) -> dict[str, Any]:
        """Generate every pending single-shot script/prompt and durably save it."""

        if self.segment_precompiler is None:
            raise LongFormError("服务端单段预编译器未配置。", "precompiler_unavailable")
        project = self.store.load(project_id)
        dirty_segments = [
            item
            for item in project["segments"]
            if not content_sync_is_current(item)
        ]
        if dirty_segments:
            indexes = "、".join(str(item["index"]) for item in dirty_segments)
            raise LongFormError(
                f"第 {indexes} 段剧情状态尚未同步。请先逐段同步；后续段请使用“重新生成后续段”。",
                "content_sync_required",
            )
        readiness = compute_render_readiness(
            project, require_authoring_confirmation=False
        )
        pending = [
            item
            for item, report in zip(project["segments"], readiness["segments"])
            if not all(report["checks"].values())
        ]
        if not pending:
            if not readiness["ready"]:
                raise LongFormError(
                    "项目内容仍未通过生成前检查："
                    + "；".join(
                        item["message"] for item in readiness["blockers"][:8]
                    ),
                    "precompile_readiness_failed",
                )
            task = self._new_task("precompile", project_id)
            self._set_task(
                task,
                state="completed",
                stage="done",
                message="全部 H3 提示词已经有效保存。",
                current=len(project["segments"]),
                total=len(project["segments"]),
            )
            task.finished_at = utc_now()
            task.updated_at = task.finished_at
            return {
                "task": task.public(),
                "project_id": project_id,
            }
        for existing in self.tasks_for_project(project_id):
            if existing.get("kind") == "precompile" and existing.get("state") in {
                "queued",
                "running",
                "retrying",
            }:
                raise LongFormError(
                    "该项目已有 H3 预编译任务正在运行。",
                    "precompile_already_running",
                )
        task = self._new_task("precompile", project_id)

        def worker() -> None:
            with self._precompile_lock:
                try:
                    self._set_task(
                        task,
                        state="running",
                        stage="preflight",
                        message="检查全部单段工作区…",
                        total=len(project["segments"]),
                    )
                    for position in range(len(project["segments"])):
                        if task.stop_requested:
                            current = self.store.load(project_id)
                            current["status"] = "paused"
                            self.store.save(current)
                            self._set_task(
                                task,
                                state="paused",
                                stage="paused",
                                message="已在下一段 Qwen 调用前暂停。",
                            )
                            return
                        current = self.store.load(project_id)
                        segment = current["segments"][position]
                        already_valid = (
                            segment.get("script_state") == "ready"
                            and segment.get("timeline_state") == "valid"
                            and segment.get("prompt_state") == "valid"
                            and (segment.get("single_workspace") or {}).get("state")
                            == "valid"
                        )
                        if already_valid:
                            self._set_task(task, current=position + 1)
                            continue
                        self._set_task(
                            task,
                            state="running",
                            stage="precompile",
                            current=position + 1,
                            message=(
                                f"生成并保存第 {position + 1}/{len(current['segments'])} "
                                "段分镜与 H3 提示词…"
                            ),
                            live_text="",
                        )

                        def provider_request(
                            messages: list[dict[str, Any]],
                        ) -> tuple[dict[str, Any], dict[str, Any]]:
                            return self._provider(messages, settings, task)

                        workspace, usage = self.segment_precompiler(
                            current, segment, provider_request
                        )
                        expected_revision = int(
                            (segment.get("single_workspace") or {}).get("revision") or 0
                        )
                        save_segment_workspace(
                            self.store,
                            current,
                            str(segment["id"]),
                            workspace,
                            expected_revision=expected_revision,
                            usage_delta=usage,
                            snapshot_reason=(
                                f"第 {position + 1} 段分镜与 H3 提示词保存完成"
                            ),
                            mark_script_change_dirty=False,
                        )
                    completed = self.store.load(project_id)
                    readiness = compute_render_readiness(
                        completed, require_authoring_confirmation=False
                    )
                    if not readiness["ready"]:
                        raise LongFormError(
                            "预编译结束但项目仍未就绪："
                            + "；".join(
                                item["message"] for item in readiness["blockers"][:8]
                            ),
                            "precompile_readiness_failed",
                        )
                    completed["status"] = "ready"
                    completed["stale_from"] = None
                    completed["scheduler"]["last_error"] = None
                    self.store.save(completed)
                    self._set_task(
                        task,
                        state="completed",
                        stage="done",
                        current=len(completed["segments"]),
                        total=len(completed["segments"]),
                        message="全部分镜和 H3 提示词均已原子保存，可以审阅。",
                        live_text="",
                    )
                except Exception as exc:
                    message, code = _provider_error(exc)
                    error = {"code": code, "message": message}
                    try:
                        failed = self.store.load(project_id)
                        failed["status"] = "failed"
                        failed["scheduler"]["last_error"] = error
                        self.store.save(failed)
                    except LongFormError:
                        pass
                    self._set_task(
                        task,
                        state="failed",
                        stage="failed",
                        error=error,
                        message=message,
                    )
                finally:
                    task.finished_at = utc_now()
                    task.updated_at = task.finished_at

        threading.Thread(
            target=worker,
            name=f"h3-long-precompile-{project_id}",
            daemon=True,
        ).start()
        return {"task": task.public(), "project_id": project_id}

    def start_render(self, project_id: str, settings: Any) -> dict[str, Any]:
        project = self.store.load(project_id)
        readiness = compute_render_readiness(project)
        if not readiness["ready"]:
            blocker_codes = {item["code"] for item in readiness["blockers"]}
            if "segment_script_not_ready" in blocker_codes:
                code = "project_not_ready"
            elif "segment_timeline_not_ready" in blocker_codes:
                code = "project_timeline_not_ready"
            elif blocker_codes & {
                "segment_prompt_not_ready",
                "segment_workspace_not_ready",
            }:
                code = "project_prompts_not_ready"
            else:
                code = str(readiness["blockers"][0]["code"])
            raise LongFormError(
                "；".join(item["message"] for item in readiness["blockers"][:8]),
                code,
            )
        if self._requires_media_backend:
            ensure_media_backend()
        task = self._new_task("render", project_id)

        def worker() -> None:
            with self._render_lock:
                try:
                    client = self.comfy_client_factory()
                    self._set_task(task, state="running", stage="preflight", message="检查 ComfyUI 节点…")
                    client.preflight()
                    project = self.store.load(project_id)
                    start_index = next(
                        (
                            index
                            for index, item in enumerate(project["segments"])
                            if item.get("render_state") != "accepted"
                        ),
                        len(project["segments"]),
                    )
                    task.total = len(project["segments"]) - start_index
                    previous_tail_path: Path | None = None
                    if start_index > 0:
                        previous = project["segments"][start_index - 1]
                        path_text = previous.get("artifacts", {}).get("tail_local")
                        previous_tail_path = Path(path_text) if path_text else None

                    for offset, segment in enumerate(project["segments"][start_index:], start=1):
                        if task.stop_requested:
                            project["status"] = "paused"
                            self.store.save(project)
                            self._set_task(task, state="paused", message="已在下一段 GPU 提交前暂停。")
                            return
                        task.current = offset
                        index = segment["index"]
                        boundary = segment["boundary_before"]
                        workspace = segment.get("single_workspace") or {}
                        form = workspace.get("form") or {}
                        mode = str(form.get("mode") or "T2VA")
                        pictures = workspace.get("pictures") or {}
                        first_frame = None
                        last_frame = None
                        picture1 = pictures.get("picture1") or {}
                        picture2 = pictures.get("picture2") or {}
                        if index == 1 and mode in {"I2VA", "FL2VA"}:
                            uploaded = self._upload_project_picture(
                                project_id, picture1, client
                            )
                            initial_name = str(
                                picture1.get("original_name") or uploaded["name"]
                            )
                            initial_description = next(
                                (
                                    str(item.get("description") or "")
                                    for item in project.get("reference_analysis") or []
                                    if isinstance(item, dict)
                                    and str(item.get("source_name") or "") == initial_name
                                ),
                                "",
                            )
                            first_frame = {
                                "type": "input",
                                "name": uploaded["name"],
                                "description": initial_description,
                            }
                        elif boundary == "continuous":
                            if previous_tail_path is None:
                                raise LongFormError("连续段缺少上一段原生尾帧。", "tail_missing")
                            first_frame = self._upload_handoff_tail(
                                project_id,
                                previous_tail_path,
                                client,
                                previous_index=index - 1,
                            )
                        elif mode in {"I2VA", "FL2VA"}:
                            uploaded = self._upload_project_picture(
                                project_id, picture1, client
                            )
                            first_frame = {
                                "type": "input",
                                "name": uploaded["name"],
                                "description": str(form.get("picture1_description") or ""),
                            }
                        if mode == "FL2VA":
                            uploaded = self._upload_project_picture(
                                project_id, picture2, client
                            )
                            last_frame = {
                                "type": "input",
                                "name": uploaded["name"],
                                "description": str(form.get("picture2_description") or ""),
                            }
                        # Schema v3 compiles only the official base T2VA/I2VA/FL2VA
                        # formats before GPU execution.  Do not silently attach
                        # Ref2VA identity slots to a base-format prompt.
                        refs: list[dict[str, str]] = []
                        accepted = False
                        prior_attempts = len(segment.get("attempts") or [])
                        for retry_index in range(1, 4):
                            attempt = prior_attempts + retry_index
                            state = "running" if retry_index == 1 else "retrying"
                            self._set_task(
                                task,
                                state=state,
                                stage="prepare",
                                message=f"第 {index} 段：使用预编译提示词准备工作流（本轮 {retry_index}/3，总尝试 {attempt}）…",
                                live_text="",
                            )
                            seed = random.SystemRandom().randrange(0, 2**63)
                            workflow = build_api_workflow(
                                prompt=segment["h3_prompt"],
                                output_frames=segment["frames"],
                                boundary_before=boundary,
                                seed=seed,
                                run_id=project_id,
                                segment_index=index,
                                attempt=attempt,
                                first_frame=first_frame,
                                last_frame=last_frame,
                                reference_images=refs,
                                project_root="$IDEA2VIDEO_PROJECT_ROOT",
                            )
                            self._set_task(
                                task,
                                stage="waiting_comfy",
                                message=f"第 {index} 段：等待 ComfyUI 空闲…",
                            )
                            project["status"] = "waiting_comfy"
                            self.store.save(project)
                            client.wait_until_idle(stop=lambda: task.stop_requested)
                            self._set_task(task, stage="gpu", message=f"第 {index} 段：H3 正在生成…")
                            prompt_id = client.submit(workflow)
                            project["status"] = "running"
                            project["scheduler"]["current_segment"] = index
                            project["scheduler"]["current_prompt_id"] = prompt_id
                            segment["render_state"] = "running"
                            segment["attempts"].append(
                                {
                                    "attempt": attempt,
                                    "seed": seed,
                                    "prompt_id": prompt_id,
                                    "submitted_at": utc_now(),
                                    "accepted": None,
                                }
                            )
                            self.store.save(project)
                            history = client.wait_history(prompt_id, stop=lambda: task.stop_requested)
                            outputs = workflow["meta"]["output_nodes"]
                            tail_receipt, tail_paths = _project_artifact_receipt(
                                history, outputs["tail"], project_root=self.store.root.parent
                            )
                            sample_receipt, sample_paths = _project_artifact_receipt(
                                history,
                                outputs["qc_samples"],
                                project_root=self.store.root.parent,
                            )
                            native_receipt, native_paths = _project_artifact_receipt(
                                history,
                                outputs["native_video"],
                                project_root=self.store.root.parent,
                            )
                            final_receipt, final_paths = _project_artifact_receipt(
                                history,
                                outputs["final_video"],
                                project_root=self.store.root.parent,
                            )
                            tail_path = tail_paths[-1]
                            native_path = native_paths[-1]
                            final_path = final_paths[-1]
                            native_media = self.media_inspector(
                                native_path,
                                expected_frames=segment["frames"],
                                expected_size=(768, 1344),
                                require_audio=True,
                            )
                            final_media = self.media_inspector(
                                final_path,
                                expected_frames=segment["frames"],
                                expected_size=(1080, 1920),
                                require_audio=True,
                            )
                            self._set_task(
                                task,
                                stage="technical_qc",
                                message=f"第 {index} 段：本地核验帧数、分辨率、音轨和尾帧…",
                            )
                            qc = {
                                "accepted": True,
                                "mode": "local_technical_only",
                                "checks": {
                                    "native_media": native_media,
                                    "final_media": final_media,
                                    "tail_exists": tail_path.is_file(),
                                },
                                "warnings": ["按项目设置，GPU 阶段未调用 Qwen 视觉质检。"],
                            }
                            segment["attempts"][-1]["accepted"] = True
                            segment["attempts"][-1]["qc"] = qc
                            segment["qc"] = qc
                            segment["render_state"] = "accepted"
                            segment["artifacts"] = {
                                "tail_local": str(tail_path),
                                "native_video": str(native_path),
                                "final_video": str(final_path),
                                "qc_samples": [str(path) for path in sample_paths],
                                "receipts": {
                                    "tail": tail_receipt,
                                    "qc_samples": sample_receipt,
                                    "native_video": native_receipt,
                                    "final_video": final_receipt,
                                },
                                "native_media": native_media,
                                "final_media": final_media,
                            }
                            previous_tail_path = tail_path
                            accepted = True
                            self.store.save(project)
                            break
                        if not accepted:
                            raise LongFormError(
                                f"第 {index} 段连续三次未通过本地技术校验，任务已暂停。",
                                "technical_retries_exhausted",
                            )

                    self._set_task(task, stage="assemble", message="合并所有已验收分段并复核总帧数与音轨…")
                    segment_paths = [
                        Path(item.get("artifacts", {}).get("final_video") or "")
                        for item in project["segments"]
                    ]
                    if any(not path.is_file() for path in segment_paths):
                        raise LongFormError("已验收分段文件不完整，无法合片。", "master_segments_missing")
                    master_path = self.store.project_dir(project_id) / "master" / f"{project_id}_master.mp4"
                    project["master"] = self.master_assembler(
                        segment_paths,
                        master_path,
                        expected_frames=project["target_frames"],
                    )
                    project["master"]["completed_at"] = utc_now()
                    project["status"] = "completed"
                    project["stale_from"] = None
                    project["scheduler"] = {
                        "current_segment": None,
                        "current_prompt_id": "",
                        "stop_after_current": False,
                        "last_error": None,
                    }
                    self.store.save(project)
                    self._set_task(
                        task,
                        state="completed",
                        stage="done",
                        message="全部分段已生成、通过本地技术校验，并合成为最终总片。",
                    )
                except Exception as exc:
                    message, code = _provider_error(exc)
                    task_state = "paused" if code in {"task_paused", "technical_retries_exhausted"} else "failed"
                    self._set_task(
                        task,
                        state=task_state,
                        message=message,
                        error={"code": code, "message": message},
                    )
                    try:
                        project = self.store.load(project_id)
                        project["status"] = task_state
                        project["scheduler"]["last_error"] = task.error
                        self.store.save(project)
                    except LongFormError:
                        pass
                finally:
                    task.finished_at = utc_now()
                    task.updated_at = task.finished_at

        threading.Thread(target=worker, name=f"h3-render-{project_id}", daemon=True).start()
        return {"task": task.public()}

    def request_stop(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise LongFormError("后台任务不存在。", "task_not_found")
            task.stop_requested = True
            task.message = "将在当前模型/API 或 GPU 段完成后暂停。"
            task.updated_at = utc_now()
            return task.public()
