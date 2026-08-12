#!/usr/bin/env python3
"""Local MiniMax H3 prompt studio powered by a project-owned LM Studio model.

The server uses Python's standard library and listens on loopback by default.
Selected keyframes are stored as content-addressed project assets; no image is
sent to LM Studio or ComfyUI until the user invokes the corresponding action.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from copy import deepcopy
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable

from longform import (
    SCHEMA_VERSION,
    LongFormError,
    LongProjectStore,
    confirm_authoring,
    edit_segment,
    edit_story_card,
    mutate_timeline,
    save_segment_workspace,
)
from longform_runtime import (
    ComfyClient,
    LongFormRuntime,
    compute_render_readiness,
    ensure_media_backend,
)
from context_runtime import ContextComfyClient, ContextLoopRuntime
from project_assets import ProjectAssetStore
from lmstudio_runtime import (
    DEFAULT_BASE_URL as LMSTUDIO_DEFAULT_BASE_URL,
    DEFAULT_IDENTIFIER as LMSTUDIO_DEFAULT_IDENTIFIER,
    DEFAULT_MODEL as LMSTUDIO_DEFAULT_MODEL,
    DEFAULT_SERVER_PORT as LMSTUDIO_DEFAULT_SERVER_PORT,
    LMStudioError,
    LMStudioSessionManager,
    LMStudioSettings,
)


PROJECT_ROOT = Path(__file__).resolve().parent
WEB_ROOT = PROJECT_ROOT / "web"
CONFIG_PATH = PROJECT_ROOT / "config.json"
SCRIPT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "scriptwriter.md"
VISION_PROMPT_PATH = PROJECT_ROOT / "prompts" / "vision.md"
COMPILER_PROMPT_PATH = PROJECT_ROOT / "prompts" / "compiler.md"
H3_SKILL_PATH = PROJECT_ROOT / "vendor" / "h3-prompt-writing" / "SKILL.md"
H3_BASE_PATH = (
    PROJECT_ROOT
    / "vendor"
    / "h3-prompt-writing"
    / "references"
    / "base-en.txt"
)
H3_REF_PATH = (
    PROJECT_ROOT
    / "vendor"
    / "h3-prompt-writing"
    / "references"
    / "ref-en.txt"
)
RUNS_ROOT = PROJECT_ROOT / "runs"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8794
DEFAULT_COMFYUI_PORT = 8188
DEFAULT_LMSTUDIO_PORT = LMSTUDIO_DEFAULT_SERVER_PORT
DEFAULT_BASE_URL = LMSTUDIO_DEFAULT_BASE_URL
DEFAULT_MODEL = LMSTUDIO_DEFAULT_MODEL
DEFAULT_IDENTIFIER = LMSTUDIO_DEFAULT_IDENTIFIER
APP_BUILD_ID = "2026.08.12-idea2video-project-output-v4"
LONG_API_VERSION = 8
ALLOWED_MODELS = {DEFAULT_MODEL}
MAX_REQUEST_BYTES = 30 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
IMAGE_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

CORE_FIELDS = (
    "integrated_multimodal_description",
    "overall_soundscape",
    "non_diegetic_music",
)
FORBIDDEN_BASE_FIELDS = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
)

TEXT_FORM_FIELDS = (
    "mode",
    "duration",
    "aspect_ratio",
    "creative_brief",
    "visual_style",
    "subjects",
    "scene_lighting",
    "action_timeline",
    "camera_motion",
    "exact_dialogue",
    "visible_text",
    "ambient_sound",
    "music",
    "extra_constraints",
    "picture1_description",
    "picture2_description",
)


class StudioError(Exception):
    """An expected error that is safe to show to the local user."""

    def __init__(self, message: str, *, code: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "status": self.status}


ProviderSettings = LMStudioSettings


@dataclass(frozen=True)
class NetworkSettings:
    """Loopback ports used by the three cooperating local services."""

    studio_port: int = DEFAULT_PORT
    lmstudio_port: int = DEFAULT_LMSTUDIO_PORT
    comfyui_port: int = DEFAULT_COMFYUI_PORT

    @property
    def lmstudio_base_url(self) -> str:
        return f"http://127.0.0.1:{self.lmstudio_port}/api/v1"

    @property
    def comfyui_base_url(self) -> str:
        return f"http://127.0.0.1:{self.comfyui_port}"

    def as_ports(self) -> dict[str, int]:
        return {
            "studio": self.studio_port,
            "lmstudio": self.lmstudio_port,
            "comfyui": self.comfyui_port,
        }


CONFIG_LOCK = threading.Lock()


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise StudioError(
            f"无法读取本地文件：{path.name}", code="local_file_error", status=500
        ) from exc


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StudioError("缺少 config.json。", code="config_missing", status=500) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StudioError(
            "config.json 无法读取或不是有效 JSON。",
            code="config_invalid",
            status=500,
        ) from exc
    if not isinstance(value, dict):
        raise StudioError("config.json 顶层必须是对象。", code="config_invalid", status=500)
    return value


def _validated_port(value: Any, *, key: str, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise StudioError(
            f"config.json 中的 {key} 必须是 1–65535 的整数。",
            code="port_invalid",
            status=500,
        )
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise StudioError(
            f"config.json 中的 {key} 必须是 1–65535 的整数。",
            code="port_invalid",
            status=500,
        ) from exc
    if isinstance(value, float) and not value.is_integer():
        port = 0
    if isinstance(value, str) and str(port) != value.strip():
        port = 0
    if not 1 <= port <= 65535:
        raise StudioError(
            f"config.json 中的 {key} 必须是 1–65535 的整数。",
            code="port_invalid",
            status=500,
        )
    return port


def _network_settings_from_config(config: dict[str, Any]) -> NetworkSettings:
    legacy_lmstudio_port: int | None = None
    if "lmstudio_port" not in config and config.get("base_url"):
        parsed = urllib.parse.urlparse(str(config["base_url"]).strip())
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port is None
            or parsed.path.rstrip("/") != "/api/v1"
        ):
            raise StudioError(
                "旧版 base_url 必须是本机 LM Studio 的 http://127.0.0.1:<端口>/api/v1。",
                code="base_url_invalid",
                status=500,
            )
        legacy_lmstudio_port = parsed.port
    settings = NetworkSettings(
        studio_port=_validated_port(
            config.get("studio_port"), key="studio_port", default=DEFAULT_PORT
        ),
        lmstudio_port=_validated_port(
            config.get("lmstudio_port", legacy_lmstudio_port),
            key="lmstudio_port",
            default=DEFAULT_LMSTUDIO_PORT,
        ),
        comfyui_port=_validated_port(
            config.get("comfyui_port"),
            key="comfyui_port",
            default=DEFAULT_COMFYUI_PORT,
        ),
    )
    ports = settings.as_ports()
    if len(set(ports.values())) != len(ports):
        raise StudioError(
            "Studio、LM Studio 和 ComfyUI 必须使用三个不同端口。",
            code="port_conflict",
            status=500,
        )
    return settings


def resolve_network_settings(config_path: Path = CONFIG_PATH) -> NetworkSettings:
    return _network_settings_from_config(load_config(config_path))


def save_port_settings(
    raw: Any,
    *,
    config_path: Path = CONFIG_PATH,
) -> NetworkSettings:
    if not isinstance(raw, dict):
        raise StudioError("端口设置必须是 JSON 对象。", code="ports_invalid")
    keys = ("studio_port", "lmstudio_port", "comfyui_port")
    with CONFIG_LOCK:
        config = load_config(config_path)
        for key in keys:
            if key not in raw:
                raise StudioError(f"缺少 {key}。", code="ports_invalid")
            config[key] = raw[key]
        if "lmstudio_auto_start" in raw:
            if not isinstance(raw["lmstudio_auto_start"], bool):
                raise StudioError(
                    "lmstudio_auto_start 必须是布尔值。", code="ports_invalid"
                )
            config["lmstudio_auto_start"] = raw["lmstudio_auto_start"]
        # ``base_url`` was the old fixed LM Studio setting.  The explicit port
        # is now the single source of truth, so do not leave a stale duplicate.
        config.pop("base_url", None)
        settings = _network_settings_from_config(config)
        config.update(
            {
                "studio_port": settings.studio_port,
                "lmstudio_port": settings.lmstudio_port,
                "comfyui_port": settings.comfyui_port,
            }
        )
        temporary = config_path.with_name(config_path.name + ".tmp")
        try:
            temporary.write_text(
                json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, config_path)
        except (OSError, UnicodeError) as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise StudioError(
                "无法写入 config.json。", code="config_write_failed", status=500
            ) from exc
    return settings


def resolve_provider_settings(
    _temporary_key: str = "",
    *,
    config_path: Path = CONFIG_PATH,
    secrets_path: Path | None = None,
) -> ProviderSettings:
    # ``_temporary_key`` and ``secrets_path`` remain accepted so old saved UI
    # requests and test helpers do not break; local LM Studio needs no API key.
    del secrets_path
    config = load_config(config_path)
    network = _network_settings_from_config(config)
    base_url = network.lmstudio_base_url
    model = str(config.get("model") or DEFAULT_MODEL).strip()
    identifier = str(config.get("identifier") or DEFAULT_IDENTIFIER).strip()
    if model not in ALLOWED_MODELS:
        raise StudioError(
            f"不支持的模型：{model}。当前仅允许 {DEFAULT_MODEL}。",
            code="model_invalid",
            status=400,
        )
    if identifier != DEFAULT_IDENTIFIER:
        raise StudioError(
            f"本项目实例名必须是 {DEFAULT_IDENTIFIER}。",
            code="identifier_invalid",
            status=500,
        )
    if int(config.get("context_length") or 0) != 131072:
        raise StudioError(
            "LM Studio context_length 必须固定为 131072（128K）。",
            code="context_length_invalid",
            status=500,
        )
    reasoning = str(config.get("reasoning") or "on").strip().lower()
    if reasoning != "on":
        raise StudioError(
            "本地小模型的原生 reasoning 必须固定为 on。",
            code="reasoning_invalid",
            status=500,
        )
    if config.get("stream") is not True:
        raise StudioError(
            "LM Studio stream 必须固定为 true。",
            code="stream_invalid",
            status=500,
        )
    auto_start_server = config.get("lmstudio_auto_start", True)
    if not isinstance(auto_start_server, bool):
        raise StudioError(
            "LM Studio lmstudio_auto_start 必须是 true 或 false。",
            code="lmstudio_auto_start_invalid",
            status=500,
        )
    return ProviderSettings(
        base_url=base_url,
        model=model,
        identifier=identifier,
        stream=bool(config.get("stream", True)),
        reasoning=reasoning,
        auto_start_server=auto_start_server,
    )


def public_config(
    *,
    config_path: Path = CONFIG_PATH,
    active_network: NetworkSettings | None = None,
    active_studio_port: int | None = None,
    active_provider_settings: ProviderSettings | None = None,
) -> dict[str, Any]:
    configured_network = resolve_network_settings(config_path)
    settings = resolve_provider_settings(config_path=config_path)
    active = active_network or configured_network
    active_ports = active.as_ports()
    if active_studio_port is not None:
        active_ports["studio"] = int(active_studio_port)
    configured_ports = configured_network.as_ports()
    active_provider = active_provider_settings or settings
    restart_required = (
        configured_ports != active_ports
        or settings.auto_start_server != active_provider.auto_start_server
    )
    return {
        "provider": "lmstudio",
        "base_url": settings.base_url,
        "model": settings.model,
        "identifier": settings.identifier,
        "stream": settings.stream,
        "thinking": True,
        "reasoning": settings.reasoning,
        "json_handling": "prompt_and_local_validation",
        "context_length": 131072,
        "api_key_required": False,
        "lmstudio_auto_start": {
            "configured": settings.auto_start_server,
            "active": active_provider.auto_start_server,
        },
        "ports": {
            "configured": configured_ports,
            "active": active_ports,
        },
        "endpoints": {
            "studio": f"http://127.0.0.1:{configured_network.studio_port}",
            "lmstudio": configured_network.lmstudio_base_url,
            "comfyui": configured_network.comfyui_base_url,
        },
        "restart_required": restart_required,
    }


def sanitize_form(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StudioError("form 必须是 JSON 对象。", code="form_invalid")
    form: dict[str, Any] = {}
    for name in TEXT_FORM_FIELDS:
        value = raw.get(name, "")
        if name == "duration":
            try:
                duration = float(value)
            except (TypeError, ValueError) as exc:
                raise StudioError("视频时长必须是数字。", code="duration_invalid") from exc
            if duration < 4 or duration > 15:
                raise StudioError("视频时长必须在 4–15 秒之间。", code="duration_invalid")
            # Long-form segments are allocated on exact 24fps frame boundaries
            # (for example 136/24 = 5.666667). Two-decimal rounding made those
            # otherwise valid workspaces impossible to save.
            form[name] = round(duration, 6)
            continue
        text = str(value or "")
        if len(text) > 100_000:
            raise StudioError(f"字段 {name} 过长。", code="form_too_large")
        form[name] = text
    mode = form["mode"].upper().strip()
    if mode not in {"T2VA", "I2VA", "FL2VA"}:
        raise StudioError("模式必须是 T2VA、I2VA 或 FL2VA。", code="mode_invalid")
    form["mode"] = mode
    form["aspect_ratio"] = form["aspect_ratio"].strip() or "9:16"
    return form


def nonblank_lines(value: str) -> list[str]:
    return [line for line in value.replace("\r\n", "\n").split("\n") if line.strip()]


def ensure_creative_input(form: dict[str, Any]) -> None:
    useful = (
        "creative_brief",
        "visual_style",
        "subjects",
        "scene_lighting",
        "action_timeline",
        "exact_dialogue",
        "extra_constraints",
    )
    if not any(str(form.get(name, "")).strip() for name in useful):
        raise StudioError("请至少填写创意概述或其他创作信息。", code="creative_input_missing")


def ensure_picture_descriptions(form: dict[str, Any]) -> None:
    mode = form["mode"]
    if mode in {"I2VA", "FL2VA"} and not form["picture1_description"].strip():
        raise StudioError(
            "I2VA/FL2VA 编译前需要 Picture 1 视觉描述；可手填或先分析图片。",
            code="picture1_description_missing",
        )
    if mode == "FL2VA" and not form["picture2_description"].strip():
        raise StudioError(
            "FL2VA 编译前需要 Picture 2 视觉描述；可手填或先分析图片。",
            code="picture2_description_missing",
        )


def build_script_messages(form: dict[str, Any]) -> list[dict[str, Any]]:
    system_prompt = read_text(SCRIPT_PROMPT_PATH)
    payload = {
        "task": "请按系统说明生成可编辑分镜剧本 JSON",
        "input": form,
        "exact_dialogue_lines": nonblank_lines(form["exact_dialogue"]),
        "exact_visible_text_lines": nonblank_lines(form["visible_text"]),
        "image_capability_notice": (
            "如果 input 中有 Picture 描述，它只是文字；本次请求没有发送图片。"
        ),
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def build_compiler_messages(
    form: dict[str, Any], script: dict[str, Any]
) -> list[dict[str, Any]]:
    system_prompt = "\n\n".join(
        (
            read_text(COMPILER_PROMPT_PATH),
            "# MiniMax 官方本地 SKILL.md（必须遵守）\n" + read_text(H3_SKILL_PATH),
            "# MiniMax 官方 base-en.txt（基础模式最高格式依据）\n"
            + read_text(H3_BASE_PATH),
        )
    )
    payload = {
        "task": "把已编辑剧本编译成指定模式的 MiniMax H3 JSON 输出",
        "mode": form["mode"],
        "duration": form["duration"],
        "aspect_ratio": form["aspect_ratio"],
        "picture_descriptions": {
            "Picture 1": form["picture1_description"],
            "Picture 2": form["picture2_description"],
        },
        "edited_script": script,
        "original_exact_dialogue_lines": nonblank_lines(form["exact_dialogue"]),
        "original_visible_text_lines": nonblank_lines(form["visible_text"]),
        "extra_constraints": form["extra_constraints"],
        "important": (
            "只使用官方基础模式三字段；这是 JSON 输出请求。图片本身未发送，"
            "只能使用 picture_descriptions 的文字。"
        ),
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _merge_usage(target: dict[str, Any], incoming: Any) -> None:
    if not isinstance(incoming, dict):
        return
    for key, value in incoming.items():
        if isinstance(value, bool):
            target[key] = value
        elif isinstance(value, int):
            target[key] = int(target.get(key, 0) or 0) + value
        elif isinstance(value, dict):
            nested = target.setdefault(key, {})
            if isinstance(nested, dict):
                _merge_usage(nested, value)
        elif value is not None:
            target[key] = value


EventCallback = Callable[[str, dict[str, Any]], None]


def request_qwen_json_stream(
    messages: list[dict[str, Any]],
    settings: ProviderSettings,
    *,
    event_callback: EventCallback | None = None,
    urlopen_func: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """Compatibility entry point; all inference still goes through LM Studio."""

    manager = LMStudioSessionManager(
        settings=settings,
        urlopen_func=urlopen_func,
    )
    return manager.provider_call(
        messages,
        settings,
        event_callback=event_callback,
        urlopen_func=urlopen_func,
    )

def detect_language(text: str) -> str:
    if re.search(r"[\u3040-\u30ff]", text):
        return "Japanese"
    if re.search(r"[\uac00-\ud7af]", text):
        return "Korean"
    if re.search(r"[\u3400-\u9fff]", text):
        return "Chinese"
    return "English"


def normalize_script_result(
    result: Any, form: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(result, dict) or not isinstance(result.get("script"), dict):
        raise StudioError("剧本 JSON 缺少 script 对象。", code="script_schema_invalid", status=502)
    raw_script = result["script"]
    raw_shots = raw_script.get("shots")
    if not isinstance(raw_shots, list) or not raw_shots:
        raise StudioError("剧本必须至少包含一个镜头。", code="script_schema_invalid", status=502)

    duration = float(form["duration"])
    shots: list[dict[str, Any]] = []
    previous_end = 0.0
    for index, raw_shot in enumerate(raw_shots, start=1):
        if not isinstance(raw_shot, dict):
            raise StudioError("剧本镜头必须是对象。", code="script_schema_invalid", status=502)
        try:
            shot_number = int(raw_shot.get("shot", index))
            start = float(raw_shot.get("start", 0))
            end = float(raw_shot.get("end", 0))
        except (TypeError, ValueError) as exc:
            raise StudioError("镜头时间必须是数字。", code="script_schema_invalid", status=502) from exc
        if shot_number != index:
            raise StudioError("镜头编号必须从 1 连续递增。", code="script_timeline_invalid", status=502)
        if start < -0.001 or end <= start or end > duration + 0.01:
            raise StudioError("镜头时间超出视频时长或起止无效。", code="script_timeline_invalid", status=502)
        if index == 1 and abs(start) > 0.01:
            raise StudioError("第一镜必须从 0.0 秒开始。", code="script_timeline_invalid", status=502)
        if index > 1:
            if start + 0.01 < previous_end:
                raise StudioError("镜头时间不能重叠。", code="script_timeline_invalid", status=502)
            if start > previous_end + 0.01:
                raise StudioError("镜头时间必须连续，不能留空档。", code="script_timeline_invalid", status=502)
            start = previous_end
        previous_end = end

        text_fields: dict[str, str] = {}
        for field in ("visual", "action", "camera", "sound", "music"):
            value = raw_shot.get(field, "")
            if not isinstance(value, str):
                raise StudioError(
                    f"镜头字段 {field} 必须是字符串，不能是数组或对象。",
                    code="script_schema_invalid",
                    status=502,
                )
            text_fields[field] = value

        raw_dialogue = raw_shot.get("dialogue") or []
        if not isinstance(raw_dialogue, list):
            raise StudioError("dialogue 必须是数组。", code="script_schema_invalid", status=502)
        dialogue: list[dict[str, str]] = []
        for item in raw_dialogue:
            if isinstance(item, str):
                text = item
                language = detect_language(text)
            elif isinstance(item, dict):
                text = str(item.get("text") or "")
                language = str(item.get("language") or detect_language(text))
            else:
                raise StudioError("对白条目格式无效。", code="script_schema_invalid", status=502)
            if text:
                dialogue.append({"language": language, "text": text})

        raw_visible = raw_shot.get("visible_text") or []
        if not isinstance(raw_visible, list):
            raise StudioError("visible_text 必须是数组。", code="script_schema_invalid", status=502)
        shots.append(
            {
                "shot": index,
                "start": round(start, 3),
                "end": round(end, 3),
                "visual": text_fields["visual"],
                "action": text_fields["action"],
                "camera": text_fields["camera"],
                "dialogue": dialogue,
                "visible_text": [str(item) for item in raw_visible if str(item)],
                "sound": text_fields["sound"],
                "music": text_fields["music"],
            }
        )
    if abs(shots[-1]["end"] - duration) > 0.01:
        raise StudioError("最后一镜结束时间必须等于视频时长。", code="script_timeline_invalid", status=502)

    dialogue_values = {
        item["text"] for shot in shots for item in shot["dialogue"]
    }
    missing_dialogue = [
        line for line in nonblank_lines(form["exact_dialogue"]) if line not in dialogue_values
    ]
    if missing_dialogue:
        raise StudioError(
            "剧本遗漏或改写了用户的精确对白。",
            code="script_dialogue_not_preserved",
            status=502,
        )
    visible_values = {item for shot in shots for item in shot["visible_text"]}
    missing_visible = [
        line for line in nonblank_lines(form["visible_text"]) if line not in visible_values
    ]
    if missing_visible:
        raise StudioError(
            "剧本遗漏或改写了用户的画面可见文字。",
            code="script_visible_text_not_preserved",
            status=502,
        )
    warnings = result.get("warnings") or []
    if not isinstance(warnings, list):
        warnings = [str(warnings)]
    return {
        "script": {
            "title": str(raw_script.get("title") or "未命名短片"),
            "logline": str(raw_script.get("logline") or ""),
            "duration": duration,
            "aspect_ratio": form["aspect_ratio"],
            "shots": shots,
        },
        "warnings": [str(item) for item in warnings],
    }


def _script_dialogues(script: dict[str, Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for shot in script.get("shots", []):
        for item in shot.get("dialogue", []):
            language = str(item.get("language") or detect_language(str(item.get("text") or "")))
            text = str(item.get("text") or "")
            if text:
                result.append((language, text))
    return result


def _script_visible_text(script: dict[str, Any]) -> list[str]:
    return [
        str(item)
        for shot in script.get("shots", [])
        for item in shot.get("visible_text", [])
        if str(item)
    ]


def validate_h3_prompt(
    mode: str,
    prompt: str,
    duration: float,
    last_shot: int,
    expected_dialogue: Iterable[tuple[str, str]],
    expected_visible_text: Iterable[str],
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    normalized = prompt.replace("\r\n", "\n").strip()
    lines = normalized.split("\n") if normalized else []

    if mode == "T2VA":
        if not normalized.startswith("integrated_multimodal_description:"):
            errors.append("T2VA 必须直接从 integrated_multimodal_description 开始。")
        if re.search(r"<?Picture\s*\d+>?|reference pictures align", normalized, re.I):
            errors.append("T2VA 不得包含任何图片引用或图片对齐指令。")
    elif mode == "I2VA":
        expected = (
            "For the target video, at 0.00 seconds into the target video, "
            "<Picture 1> (from [Shot 1]) is fully referenced."
        )
        if not lines or lines[0] != expected:
            errors.append("I2VA 第一行不是官方固定格式。")
        if len(lines) < 2 or lines[1] != "":
            errors.append("I2VA 对齐指令后必须有一个空行。")
    else:
        expected = (
            "How the reference pictures align with the target video — Picture 1 "
            "(from Shot 1) aligns with the 0.00-second mark of the target video; "
            f"Picture 2 (from Shot {last_shot}) aligns with the {duration:.2f}-second "
            "mark of the target video."
        )
        if not lines or lines[0] != expected:
            errors.append("FL2VA 第一行、末镜编号或结束时间不是官方固定格式。")
        if len(lines) < 2 or lines[1] != "":
            errors.append("FL2VA 对齐指令后必须有一个空行。")

    field_positions: list[int] = []
    for field in CORE_FIELDS:
        matches = list(re.finditer(rf"(?m)^{re.escape(field)}:", normalized))
        if len(matches) != 1:
            errors.append(f"字段 {field} 必须且只能出现一次。")
            field_positions.append(-1)
        else:
            field_positions.append(matches[0].start())
    if all(position >= 0 for position in field_positions) and field_positions != sorted(field_positions):
        errors.append("三个核心字段顺序错误。")

    for field in FORBIDDEN_BASE_FIELDS:
        if re.search(rf"(?mi)^{re.escape(field)}\s*:", normalized):
            errors.append(f"基础模式不得包含 Ref2VA 字段 {field}。")
    if re.search(r"<(?:Subject|Video|Audio)\s+\d+>", normalized, re.I):
        errors.append("基础模式不得包含 Subject、Video 或 Audio 引用标签。")

    picture_numbers = {int(value) for value in re.findall(r"Picture\s*(\d+)", normalized, re.I)}
    allowed_pictures = {"T2VA": set(), "I2VA": {1}, "FL2VA": {1, 2}}[mode]
    if not picture_numbers.issubset(allowed_pictures):
        errors.append("提示词引用了当前模式未定义的 Picture。")

    for minute, second, millis in re.findall(r"\b(\d{2}):(\d{2})\.(\d{3})\b", normalized):
        timestamp = int(minute) * 60 + int(second) + int(millis) / 1000
        if timestamp > duration + 0.0001:
            errors.append(f"时间戳 {minute}:{second}.{millis} 超过视频时长。")
    for value in re.findall(r"(\d+(?:\.\d+)?)-second mark", normalized):
        # The official FL2VA alignment line requires two decimals, while a
        # frame-exact 24fps duration may repeat (136/24 = 5.666667). Compare
        # against the same two-decimal representation required above.
        if float(value) > round(duration, 2) + 0.0001:
            errors.append(f"图片对齐时间 {value} 秒超过视频时长。")

    open_count = normalized.count("<d>")
    close_count = normalized.count("</d>")
    if open_count != close_count:
        errors.append("对白 <d> 标签不完整。")
    blocks = re.findall(r"<d>(.*?)</d>", normalized, re.DOTALL)
    parsed_blocks: list[tuple[str, str]] = []
    for block in blocks:
        match = re.fullmatch(r"\[([A-Za-z][A-Za-z -]*)\] (.+)", block, re.DOTALL)
        if not match:
            errors.append("对白必须使用 <d>[Language] 原始对白</d> 格式。")
            continue
        parsed_blocks.append((match.group(1), match.group(2)))

    expected_dialogue_list = list(expected_dialogue)
    for language, text in expected_dialogue_list:
        expected_block = f"<d>[{language}] {text}</d>"
        if expected_block not in normalized:
            errors.append(f"对白未按原文和语言标签完整保留：{text}")
    allowed_dialogue = set(expected_dialogue_list)
    for item in parsed_blocks:
        if item not in allowed_dialogue:
            errors.append(f"最终提示词出现剧本中没有的对白：{item[1]}")

    for text in expected_visible_text:
        if f'"{text}"' not in normalized:
            errors.append(f"画面可见文字未原样保留：{text}")

    shot_numbers = [int(value) for value in re.findall(r"\[Shot\s+(\d+)\]", normalized)]
    if not shot_numbers or shot_numbers[0] != 1:
        errors.append("integrated_multimodal_description 必须从 [Shot 1] 开始。")
    elif max(shot_numbers) != last_shot:
        errors.append("提示词的最后镜头编号与剧本不一致。")
    if duration != 7.0:
        warnings.append("随附的三套 ComfyUI 工作流固定为 7 秒；当前提示词时长不同。")
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def normalize_compile_result(
    raw: Any, form: dict[str, Any], script: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StudioError("H3 结果必须是 JSON 对象。", code="compile_schema_invalid", status=502)
    mode = str(raw.get("mode") or "")
    prompt = str(raw.get("prompt") or "")
    warnings = raw.get("warnings") or []
    if not isinstance(warnings, list):
        warnings = [str(warnings)]
    if mode != form["mode"]:
        local_mode_error = f"模型返回模式 {mode or '空'}，请求模式是 {form['mode']}。"
    else:
        local_mode_error = ""
    validation = validate_h3_prompt(
        form["mode"],
        prompt,
        float(form["duration"]),
        len(script["shots"]),
        _script_dialogues(script),
        _script_visible_text(script),
    )
    if local_mode_error:
        validation["errors"].insert(0, local_mode_error)
        validation["valid"] = False
    return {
        "mode": mode,
        "prompt": prompt,
        "warnings": [str(item) for item in warnings],
        "validation": validation,
    }


def precompile_segment_workspace(
    project: dict[str, Any],
    segment: dict[str, Any],
    provider_request: Callable[
        [list[dict[str, Any]]], tuple[dict[str, Any], dict[str, Any]]
    ],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the existing single-shot stages and return one validated workspace."""

    workspace = deepcopy(segment.get("single_workspace") or {})
    form = sanitize_form(workspace.get("form"))
    expected_duration = int(segment["frames"]) / 24
    if abs(float(form["duration"]) - expected_duration) > 0.0005:
        raise StudioError(
            "单段工作区时长与剧情卡片帧数不一致。",
            code="single_workspace_duration_mismatch",
        )
    form["duration"] = expected_duration
    ensure_creative_input(form)
    ensure_picture_descriptions(form)
    script_messages = build_script_messages(form)
    script_usage: dict[str, Any] = {}
    normalized_script: dict[str, Any] | None = None
    if workspace.get("preserve_script_on_precompile") and isinstance(
        workspace.get("script"), dict
    ):
        normalized_script = normalize_script_result(
            {"script": workspace["script"], "warnings": []},
            form,
        )
    else:
        for attempt in range(2):
            raw_script, attempt_usage = provider_request(script_messages)
            _merge_usage(script_usage, attempt_usage)
            try:
                normalized_script = normalize_script_result(raw_script, form)
                break
            except StudioError as exc:
                if attempt >= 1:
                    raise
                script_messages.extend(
                    [
                        {
                            "role": "assistant",
                            "content": json.dumps(raw_script, ensure_ascii=False),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "task": "repair_single_segment_script_json",
                                    "validation_error": {
                                        "code": exc.code,
                                        "message": exc.message,
                                    },
                                    "requirements": [
                                        "Return the complete script JSON again, not a patch.",
                                        "Every visual/action/camera/sound/music field must be a string, never an array or object.",
                                        "Shots must continuously cover 0.000 through the exact requested duration with no gap, overlap, or overrun.",
                                        "Preserve every exact dialogue and visible-text line byte-for-byte.",
                                    ],
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ]
                )
    if normalized_script is None:  # pragma: no cover - loop invariant
        raise AssertionError("script normalization did not complete")
    script = normalized_script["script"]
    compile_messages = build_compiler_messages(form, script)
    compile_usage: dict[str, Any] = {}
    compiled: dict[str, Any] | None = None
    for attempt in range(2):
        raw_compile, attempt_usage = provider_request(compile_messages)
        _merge_usage(compile_usage, attempt_usage)
        candidate = normalize_compile_result(raw_compile, form, script)
        validation = candidate["validation"]
        if validation.get("valid"):
            compiled = candidate
            break
        if attempt >= 1:
            raise StudioError(
                "H3 提示词两次均未通过本地校验："
                + "；".join(str(item) for item in validation.get("errors") or []),
                code="compile_validation_failed",
                status=502,
            )
        compile_messages.extend(
            [
                {
                    "role": "assistant",
                    "content": json.dumps(raw_compile, ensure_ascii=False),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": "repair_minimax_h3_prompt_json",
                            "validation_errors": validation.get("errors") or [],
                            "requirements": [
                                "Return the complete JSON object again, not a patch.",
                                "Keep the requested T2VA/I2VA/FL2VA mode and official base-en three-field order.",
                                "Preserve exact dialogue, lyrics, and visible text byte-for-byte.",
                                "Do not use Ref2VA six-section fields or undefined Picture/Subject references.",
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
        )
    if compiled is None:  # pragma: no cover - loop invariant
        raise AssertionError("prompt compilation did not complete")
    validation = compiled["validation"]
    workspace["form"] = form
    workspace["script"] = script
    workspace["prompt"] = compiled["prompt"]
    workspace["validation"] = validation
    workspace["warnings"] = list(normalized_script.get("warnings") or []) + list(
        compiled.get("warnings") or []
    )
    workspace["state"] = "valid"
    workspace.pop("preserve_script_on_precompile", None)
    workspace["usage"] = {
        "script": script_usage,
        "compile": compile_usage,
    }
    combined_usage: dict[str, Any] = {}
    _merge_usage(combined_usage, script_usage)
    _merge_usage(combined_usage, compile_usage)
    return workspace, combined_usage


def workspace_save_receipt(
    project: dict[str, Any], segment_id: str
) -> dict[str, Any]:
    segment = next(item for item in project["segments"] if item["id"] == segment_id)
    workspace = segment.get("single_workspace") or {}
    prompt = str(workspace.get("prompt") or "")
    return {
        "project_id": project["id"],
        "segment_id": segment_id,
        "saved_at": workspace.get("updated_at") or project.get("updated_at"),
        "workspace_revision": int(workspace.get("revision") or 0),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if prompt
        else "",
        "states": {
            "script": segment.get("script_state"),
            "timeline": segment.get("timeline_state"),
            "prompt": segment.get("prompt_state"),
            "workspace": workspace.get("state"),
        },
        "readiness": compute_render_readiness(project),
    }


def save_bound_stream_result(
    store: LongProjectStore,
    *,
    binding: Any,
    form: dict[str, Any],
    operation: str,
    result: dict[str, Any],
    script: dict[str, Any] | None,
    pictures: Any = None,
    usage: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Atomically persist a bound single-shot SSE result before reporting done."""

    if not isinstance(binding, dict):
        return None
    project_id = str(binding.get("project_id") or "")
    segment_id = str(binding.get("segment_id") or "")
    if not project_id or not segment_id:
        raise StudioError(
            "长视频绑定缺少 project_id 或 segment_id。",
            code="single_workspace_binding_invalid",
        )
    try:
        project = store.load(project_id)
        target = next(
            (item for item in project["segments"] if item["id"] == segment_id),
            None,
        )
        if target is None:
            raise LongFormError("分段不存在。", "segment_not_found")
        expected_duration = int(target["frames"]) / 24
        if abs(float(form["duration"]) - expected_duration) > 0.0005:
            raise LongFormError(
                "单段工作区时长必须等于剧情卡片的精确帧时长。",
                "single_workspace_duration_mismatch",
            )
        bound_form = deepcopy(form)
        bound_form["duration"] = expected_duration
        workspace = deepcopy(target.get("single_workspace") or {})
        workspace["form"] = bound_form
        if isinstance(pictures, dict):
            workspace["pictures"] = deepcopy(pictures)
        if operation == "script":
            workspace["script"] = deepcopy(result["script"])
            workspace["prompt"] = ""
            workspace["validation"] = {
                "valid": False,
                "errors": ["分镜已更新，需要重新编译 H3 提示词。"],
                "warnings": [],
            }
            workspace["warnings"] = list(result.get("warnings") or [])
            workspace["state"] = "draft"
        elif operation == "compile":
            workspace["script"] = deepcopy(script)
            workspace["prompt"] = str(result.get("prompt") or "")
            workspace["validation"] = deepcopy(result.get("validation") or {})
            workspace["warnings"] = list(result.get("warnings") or [])
            workspace["state"] = (
                "valid" if workspace["validation"].get("valid") is True else "draft"
            )
        else:
            return None
        workspace.setdefault("usage", {})[operation] = deepcopy(usage or {})
        expected_raw = binding.get("workspace_revision")
        expected_revision = int(expected_raw) if expected_raw is not None else None
        updated = save_segment_workspace(
            store,
            project,
            segment_id,
            workspace,
            expected_revision=expected_revision,
            usage_delta=deepcopy(usage or {}),
            snapshot_reason=f"{segment_id} {operation} 原子保存完成",
            mark_script_change_dirty=False,
        )
        return updated, workspace_save_receipt(updated, segment_id)
    except LongFormError as exc:
        raise StudioError(
            exc.message,
            code=exc.code,
            status=409 if exc.code == "single_workspace_conflict" else 400,
        ) from exc


DATA_URL_RE = re.compile(
    r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\r\n]+)$",
    re.IGNORECASE,
)


def _validated_image_data(mime: str, decoded: bytes) -> str:
    if not decoded or len(decoded) > MAX_IMAGE_BYTES:
        raise StudioError("每张图片必须小于等于 10 MiB。", code="image_size_invalid")
    magic_ok = (
        (mime == "image/png" and decoded.startswith(b"\x89PNG\r\n\x1a\n"))
        or (mime == "image/jpeg" and decoded.startswith(b"\xff\xd8\xff"))
        or (
            mime == "image/webp"
            and decoded.startswith(b"RIFF")
            and decoded[8:12] == b"WEBP"
        )
    )
    if not magic_ok:
        raise StudioError("图片内容与声明格式不一致。", code="image_type_invalid")
    return f"data:{mime};base64,{base64.b64encode(decoded).decode('ascii')}"


def validate_images(
    raw_images: Any,
    mode: str,
) -> list[dict[str, str]]:
    if mode == "T2VA":
        raise StudioError("T2VA 不需要参考图分析。", code="images_not_allowed")
    expected_count = 1 if mode == "I2VA" else 2
    if not isinstance(raw_images, list) or len(raw_images) != expected_count:
        raise StudioError(
            f"{mode} 分析需要 {expected_count} 张图片。",
            code="image_count_invalid",
        )
    result: list[dict[str, str]] = []
    for index, item in enumerate(raw_images, start=1):
        if not isinstance(item, dict):
            raise StudioError("图片数据格式无效。", code="image_invalid")
        data_url = str(item.get("data_url") or "")
        match = DATA_URL_RE.fullmatch(data_url)
        if not match:
            raise StudioError(
                "只接受 PNG、JPEG 或 WebP 图片。",
                code="image_type_invalid",
            )
        try:
            decoded = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise StudioError("图片 Base64 数据损坏。", code="image_invalid") from exc
        mime = match.group(1).lower()
        result.append(
            {
                "id": f"Picture {index}",
                "data_url": _validated_image_data(mime, decoded),
            }
        )
    return result


def build_vision_messages(
    form: dict[str, Any], images: list[dict[str, str]]
) -> list[dict[str, Any]]:
    system_prompt = read_text(VISION_PROMPT_PATH)
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": json.dumps(
                {
                    "task": "按图片顺序分析 Picture 1/2，并输出系统规定的 JSON",
                    "mode": form["mode"],
                    "duration": form["duration"],
                    "creative_context_only": form["creative_brief"],
                    "important": "保持客观，不写剧本，不输出 H3 提示词。",
                },
                ensure_ascii=False,
            ),
        }
    ]
    for image in images:
        content.append({"type": "image_url", "image_url": {"url": image["data_url"]}})
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]


def normalize_vision_result(raw: Any, mode: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("pictures"), list):
        raise StudioError("图片分析 JSON 缺少 pictures 数组。", code="vision_schema_invalid", status=502)
    expected_count = 1 if mode == "I2VA" else 2
    if len(raw["pictures"]) != expected_count:
        raise StudioError("图片分析结果数量不正确。", code="vision_schema_invalid", status=502)
    pictures: list[dict[str, Any]] = []
    for index, item in enumerate(raw["pictures"], start=1):
        if not isinstance(item, dict):
            raise StudioError("图片分析条目格式无效。", code="vision_schema_invalid", status=502)
        expected_id = f"Picture {index}"
        description = str(item.get("description") or "").strip()
        if not description:
            raise StudioError("图片分析缺少视觉描述。", code="vision_schema_invalid", status=502)
        visible_text = item.get("visible_text") or []
        if not isinstance(visible_text, list):
            visible_text = [str(visible_text)]
        pictures.append(
            {
                "id": expected_id,
                "description": description,
                "visible_text": [str(value) for value in visible_text if str(value)],
            }
        )
    warnings = raw.get("warnings") or []
    if not isinstance(warnings, list):
        warnings = [str(warnings)]
    return {
        "pictures": pictures,
        "transition_observations": str(raw.get("transition_observations") or ""),
        "warnings": [str(item) for item in warnings],
    }


class StudioHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR permits two live listeners on the same endpoint on Windows,
    # which made requests randomly hit an old or new Studio process. Exclusive
    # binding is safer here; start.bat already probes an existing service.
    allow_reuse_address = os.name != "nt"

    long_store: LongProjectStore
    long_runtime: LongFormRuntime
    context_runtime: ContextLoopRuntime
    provider_call: Callable[..., Any]
    lmstudio_manager: LMStudioSessionManager
    config_path: Path
    network_settings: NetworkSettings
    provider_settings: ProviderSettings
    asset_store: ProjectAssetStore


class StudioIPv6HTTPServer(StudioHTTPServer):
    address_family = socket.AF_INET6


class StudioHandler(BaseHTTPRequestHandler):
    server_version = "H3PromptStudio/1.0"

    def log_message(self, _format: str, *args: Any) -> None:
        # Log method and path only. Request bodies and credentials are never logged.
        path = urllib.parse.urlsplit(self.path).path
        sys.stderr.write(f"[HTTP] {self.command} {path}\n")

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )

    @staticmethod
    def _loopback_hostname(value: str) -> str | None:
        try:
            parsed = urllib.parse.urlsplit("//" + str(value or ""))
            if (
                parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                return None
            _ = parsed.port  # Validate an optional numeric port.
            hostname = (parsed.hostname or "").lower()
        except ValueError:
            return None
        return hostname if hostname in LOOPBACK_HOSTS else None

    def _validate_request_context(self, *, require_json: bool = False) -> bool:
        """Reject DNS-rebinding, cross-site and simple no-CORS write requests."""

        if self._loopback_hostname(self.headers.get("Host", "")) is None:
            self._send_json(
                403,
                {
                    "error": {
                        "code": "local_request_required",
                        "message": "Studio 只接受本机 loopback 请求。",
                        "status": 403,
                    }
                },
            )
            return False
        origin = self.headers.get("Origin")
        if origin:
            try:
                parsed = urllib.parse.urlsplit(origin)
                origin_scheme = parsed.scheme
                origin_host = (parsed.hostname or "").lower()
                origin_port = parsed.port
            except ValueError:
                origin_scheme = ""
                origin_host = ""
                origin_port = None
            active_port = int(self.server.server_address[1])
            if (
                origin_scheme != "http"
                or origin_host not in LOOPBACK_HOSTS
                or origin_port != active_port
            ):
                self._send_json(
                    403,
                    {
                        "error": {
                            "code": "cross_origin_forbidden",
                            "message": "Studio 拒绝跨来源请求。",
                            "status": 403,
                        }
                    },
                )
                return False
        if require_json and self.headers.get_content_type().lower() != "application/json":
            self._send_json(
                415,
                {
                    "error": {
                        "code": "json_content_type_required",
                        "message": "写操作必须使用 application/json。",
                        "status": 415,
                    }
                },
            )
            return False
        return True

    def _send_json(self, status: int, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _send_file(self, path: Path, *, download_name: str) -> None:
        data = Path(path).read_bytes()
        quoted = urllib.parse.quote(download_name, safe="._-")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted}")
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            raise StudioError("缺少 Content-Length。", code="request_invalid")
        try:
            length = int(length_header)
        except ValueError as exc:
            raise StudioError("Content-Length 无效。", code="request_invalid") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise StudioError("请求体为空或超过 30 MiB。", code="request_too_large", status=413)
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise StudioError("请求体不是有效 UTF-8 JSON。", code="request_invalid") from exc
        if not isinstance(value, dict):
            raise StudioError("请求 JSON 顶层必须是对象。", code="request_invalid")
        return value

    def _start_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self._security_headers()
        self.end_headers()
        self.close_connection = True

    def _event(self, name: str, value: dict[str, Any]) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        packet = f"event: {name}\ndata: {data}\n\n".encode("utf-8")
        self.wfile.write(packet)
        self.wfile.flush()

    def do_GET(self) -> None:  # noqa: N802
        if not self._validate_request_context():
            return
        request_url = urllib.parse.urlsplit(self.path)
        path = request_url.path
        try:
            if path == "/api/health":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "service": "MiniMax H3 Prompt Studio",
                        "provider": "lmstudio",
                        "model": DEFAULT_MODEL,
                        "build_id": APP_BUILD_ID,
                        "long_api_version": LONG_API_VERSION,
                        "project_schema_version": SCHEMA_VERSION,
                        "listen": f"{self.server.server_address[0]}:{self.server.server_address[1]}",
                    },
                )
                return
            if path == "/api/config":
                self._send_json(
                    200,
                    public_config(
                        config_path=self.server.config_path,
                        active_network=self.server.network_settings,
                        active_studio_port=int(self.server.server_address[1]),
                        active_provider_settings=self.server.provider_settings,
                    ),
                )
                return
            if path == "/api/lmstudio/status":
                self._send_json(200, self.server.lmstudio_manager.status())
                return
            asset_match = re.fullmatch(
                r"/api/long/projects/([A-Za-z0-9_-]+)/assets/([0-9a-f]{64})",
                path,
            )
            if asset_match:
                image_path, mime = self.server.asset_store.find(*asset_match.groups())
                data = image_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self._security_headers()
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/long/projects":
                self._send_json(200, {"projects": self.server.long_store.list_projects()})
                return
            if path == "/api/comfy/status":
                ensure_media_backend()
                client = self.server.long_runtime.comfy_client_factory()
                preflight = client.preflight()
                queue = client.queue()
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "preflight": preflight,
                        "media_backend": "PyAV",
                        "queue_running": len(queue.get("queue_running") or []),
                        "queue_pending": len(queue.get("queue_pending") or []),
                        "base_url": client.base_url,
                    },
                )
                return
            if path == "/api/context-loop/plugin-status":
                self._send_json(200, self.server.context_runtime.plugin_status())
                return
            context_artifact_match = re.fullmatch(
                r"/api/long/projects/([A-Za-z0-9_-]+)/context-loop/artifacts/"
                r"(spec|plan|api_prompt|workflow)",
                path,
            )
            if context_artifact_match:
                project_id, kind = context_artifact_match.groups()
                artifact = self.server.context_runtime.artifact_path(project_id, kind)
                self._send_file(
                    artifact,
                    download_name=f"{project_id}_context_loop_{kind}.json",
                )
                return
            context_match = re.fullmatch(
                r"/api/long/projects/([A-Za-z0-9_-]+)/context-loop", path
            )
            if context_match:
                self._send_json(
                    200,
                    self.server.context_runtime.public(context_match.group(1)),
                )
                return
            readiness_match = re.fullmatch(
                r"/api/long/projects/([A-Za-z0-9_-]+)/readiness", path
            )
            if readiness_match:
                project = self.server.long_store.load(readiness_match.group(1))
                self._send_json(200, {"readiness": compute_render_readiness(project)})
                return
            long_match = re.fullmatch(r"/api/long/projects/([A-Za-z0-9_-]+)", path)
            if long_match:
                project_id = long_match.group(1)
                project = self.server.long_store.load(project_id)
                self._send_json(
                    200,
                    {
                        "project": project,
                        "tasks": self.server.long_runtime.tasks_for_project(project_id),
                        "readiness": compute_render_readiness(project),
                    },
                )
                return
            task_match = re.fullmatch(r"/api/long/tasks/([A-Za-z0-9_-]+)", path)
            if task_match:
                task_id = task_match.group(1)
                runtime = (
                    self.server.context_runtime
                    if task_id.startswith("context_")
                    else self.server.long_runtime
                )
                self._send_json(
                    200,
                    {"task": runtime.task(task_id)},
                )
                return
            static_files = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/index.html": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/style.css": ("style.css", "text/css; charset=utf-8"),
            }
            item = static_files.get(path)
            if not item:
                self._send_json(404, {"error": {"code": "not_found", "message": "未找到。"}})
                return
            data = (WEB_ROOT / item[0]).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", item[1])
            self.send_header("Content-Length", str(len(data)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(data)
        except StudioError as exc:
            self._send_json(exc.status, {"error": exc.as_dict()})
        except LongFormError as exc:
            status = 404 if exc.code.endswith("not_found") else 400
            self._send_json(
                status,
                {"error": {"code": exc.code, "message": exc.message, "status": status}},
            )
        except OSError:
            self._send_json(
                500,
                {"error": {"code": "static_file_error", "message": "网页文件无法读取。"}},
            )

    def do_PUT(self) -> None:  # noqa: N802
        if not self._validate_request_context(require_json=True):
            return
        path = urllib.parse.urlsplit(self.path).path
        context_match = re.fullmatch(
            r"/api/long/projects/([A-Za-z0-9_-]+)/context-loop", path
        )
        if not context_match:
            self._send_json(404, {"error": {"code": "not_found", "message": "未找到。"}})
            return
        try:
            payload = self._read_json()
            if not isinstance(payload.get("context_loop"), dict):
                raise StudioError("缺少 context_loop JSON 对象。", code="request_invalid")
            if payload.get("expected_revision") is None:
                raise StudioError("缺少 expected_revision。", code="request_invalid")
            result = self.server.context_runtime.save_spec(
                context_match.group(1),
                payload["context_loop"],
                expected_revision=int(payload["expected_revision"]),
            )
            self._send_json(200, result)
        except StudioError as exc:
            self._send_json(exc.status, {"error": exc.as_dict()})
        except LongFormError as exc:
            status = 409 if exc.code == "context_revision_conflict" else 400
            self._send_json(
                status,
                {"error": {"code": exc.code, "message": exc.message, "status": status}},
            )
        except (TypeError, ValueError):
            self._send_json(
                400,
                {"error": {"code": "request_invalid", "message": "规则 JSON 请求参数无效。", "status": 400}},
            )

    def do_POST(self) -> None:  # noqa: N802
        if not self._validate_request_context(require_json=True):
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/settings/ports":
            self._handle_port_settings()
            return
        if path in {
            "/api/lmstudio/server/start",
            "/api/lmstudio/session/start",
            "/api/lmstudio/session/release",
        }:
            self._handle_lmstudio_session(path)
            return
        if path.startswith("/api/long/"):
            self._handle_long_post(path)
            return
        if path not in {
            "/api/script/stream",
            "/api/analyze-images/stream",
            "/api/compile/stream",
        }:
            self._send_json(404, {"error": {"code": "not_found", "message": "未找到。"}})
            return
        try:
            payload = self._read_json()
            form = sanitize_form(payload.get("form"))
            settings = self.server.provider_settings
            if path == "/api/script/stream":
                ensure_creative_input(form)
                messages = build_script_messages(form)
                operation = "script"
                context: Any = None
            elif path == "/api/analyze-images/stream":
                images = validate_images(payload.get("images"), form["mode"])
                messages = build_vision_messages(form, images)
                operation = "vision"
                context = None
            else:
                ensure_picture_descriptions(form)
                incoming_script = payload.get("script")
                try:
                    normalized = normalize_script_result(
                        {"script": incoming_script, "warnings": []}, form
                    )
                except StudioError as exc:
                    if exc.code.startswith("script_"):
                        raise StudioError(
                            exc.message, code=exc.code, status=400
                        ) from exc
                    raise
                context = normalized["script"]
                messages = build_compiler_messages(form, context)
                operation = "compile"
        except StudioError as exc:
            self._send_json(exc.status, {"error": exc.as_dict()})
            return

        self._start_sse()
        try:
            self._event(
                "status",
                {
                    "stage": operation,
                    "message": "LM Studio 本地 Qwen 正在思考并流式生成…",
                },
            )
            raw, usage, finish_reason = self.server.provider_call(
                messages, settings, event_callback=self._event
            )
            if operation == "script":
                result = normalize_script_result(raw, form)
            elif operation == "vision":
                result = normalize_vision_result(raw, form["mode"])
            else:
                result = normalize_compile_result(raw, form, context)
            saved = save_bound_stream_result(
                self.server.long_store,
                binding=payload.get("binding"),
                form=form,
                operation=operation,
                result=result,
                script=context if operation == "compile" else result.get("script"),
                pictures=payload.get("workspace_pictures"),
                usage=usage,
            )
            if saved is not None:
                saved_project, receipt = saved
                self._event(
                    "saved",
                    {"receipt": receipt, "project": saved_project},
                )
            self._event("usage", {"usage": usage})
            self._event(
                "result",
                {"operation": operation, "result": result, "finish_reason": finish_reason},
            )
            self._event("done", {"ok": True})
        except (StudioError, LMStudioError) as exc:
            try:
                value = (
                    exc.as_dict()
                    if isinstance(exc, StudioError)
                    else {"code": exc.code, "message": exc.message, "status": exc.status}
                )
                self._event("error", value)
                self._event("done", {"ok": False})
            except (BrokenPipeError, ConnectionResetError):
                pass
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            # Never expose provider internals in a streamed browser error.
            try:
                self._event(
                    "error",
                    {
                        "code": "internal_error",
                        "message": "本地服务发生未预期错误。",
                        "status": 500,
                    },
                )
                self._event("done", {"ok": False})
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _handle_port_settings(self) -> None:
        try:
            payload = self._read_json()
            save_port_settings(payload, config_path=self.server.config_path)
            config = public_config(
                config_path=self.server.config_path,
                active_network=self.server.network_settings,
                active_studio_port=int(self.server.server_address[1]),
                active_provider_settings=self.server.provider_settings,
            )
            self._send_json(
                200,
                {
                    "ok": True,
                    "config": config,
                    "message": (
                        "端口已保存到 config.json；重启 Idea2Video 后生效。"
                        if config["restart_required"]
                        else "端口设置未发生变化。"
                    ),
                },
            )
        except StudioError as exc:
            status = 400 if exc.code in {"ports_invalid", "port_invalid", "port_conflict"} else exc.status
            value = exc.as_dict()
            value["status"] = status
            self._send_json(status, {"error": value})

    def _handle_lmstudio_session(self, path: str) -> None:
        try:
            payload = self._read_json()
            if path == "/api/lmstudio/server/start":
                status = self.server.lmstudio_manager.ensure_server()
                self._send_json(200, {"ok": True, "status": status})
                return
            if path == "/api/lmstudio/session/start":
                status = self.server.lmstudio_manager.ensure_ready()
                self._send_json(200, {"ok": True, "status": status})
                return
            mode = str(payload.get("mode") or "pause")
            if mode not in {"pause", "confirm"}:
                raise StudioError(
                    "释放模式必须是 pause 或 confirm。",
                    code="lmstudio_release_mode_invalid",
                )
            project_id = str(payload.get("project_id") or "").strip()
            project: dict[str, Any] | None = None
            if mode == "confirm":
                if not project_id:
                    raise StudioError(
                        "确认创作完成时必须指定长视频项目。",
                        code="project_id_required",
                    )
                project = self.server.long_store.load(project_id)
                active_states = {"queued", "running", "retrying"}
                active = [
                    item
                    for item in self.server.long_runtime.tasks_for_project(project_id)
                    if item.get("state") in active_states
                ]
                active += [
                    item
                    for item in self.server.context_runtime.tasks_for_project(project_id)
                    if item.get("state") in active_states
                ]
                if active:
                    raise StudioError(
                        "项目仍有后台任务在运行，不能确认或释放显存。",
                        code="authoring_tasks_active",
                        status=409,
                    )
                readiness = compute_render_readiness(
                    project, require_authoring_confirmation=False
                )
                if not readiness["ready"]:
                    raise StudioError(
                        "；".join(item["message"] for item in readiness["blockers"][:8]),
                        code="authoring_not_ready",
                        status=409,
                    )
            status = self.server.lmstudio_manager.release()
            confirmation: dict[str, Any] | None = None
            readiness: dict[str, Any] | None = None
            if project is not None:
                # The fingerprint is written only after successful, verified unload.
                confirmation = confirm_authoring(
                    project, provider="lmstudio", model=DEFAULT_MODEL
                )
                self.server.long_store.save(project)
                readiness = compute_render_readiness(project)
            self._send_json(
                200,
                {
                    "ok": True,
                    "mode": mode,
                    "status": status,
                    "confirmation": confirmation,
                    "project": project,
                    "readiness": readiness,
                },
            )
        except StudioError as exc:
            self._send_json(exc.status, {"error": exc.as_dict()})
        except LongFormError as exc:
            status = 404 if exc.code.endswith("not_found") else 409
            self._send_json(
                status,
                {"error": {"code": exc.code, "message": exc.message, "status": status}},
            )
        except LMStudioError as exc:
            self._send_json(
                exc.status,
                {"error": {"code": exc.code, "message": exc.message, "status": exc.status}},
            )
        except Exception:
            self._send_json(
                500,
                {"error": {"code": "internal_error", "message": "本地服务发生未预期错误。", "status": 500}},
            )

    def _handle_long_post(self, path: str) -> None:
        try:
            payload = self._read_json()
            if path == "/api/long/projects":
                settings = self.server.provider_settings
                result = self.server.long_runtime.start_new_project(payload, settings)
                self._send_json(202, result)
                return

            asset_match = re.fullmatch(
                r"/api/long/projects/([A-Za-z0-9_-]+)/assets", path
            )
            if asset_match:
                asset = self.server.asset_store.save_data_url(
                    asset_match.group(1),
                    original_name=str(payload.get("name") or "image"),
                    data_url=str(payload.get("data_url") or ""),
                )
                self._send_json(
                    201,
                    {
                        "asset": self.server.asset_store.public(
                            asset_match.group(1), asset
                        )
                    },
                )
                return

            stop_match = re.fullmatch(r"/api/long/tasks/([A-Za-z0-9_-]+)/stop", path)
            if stop_match:
                task_id = stop_match.group(1)
                runtime = (
                    self.server.context_runtime
                    if task_id.startswith("context_")
                    else self.server.long_runtime
                )
                self._send_json(
                    200,
                    {"task": runtime.request_stop(task_id)},
                )
                return

            context_action_match = re.fullmatch(
                r"/api/long/projects/([A-Za-z0-9_-]+)/context-loop/(generate|render)",
                path,
            )
            if context_action_match:
                project_id, action = context_action_match.groups()
                if action == "generate":
                    result = self.server.context_runtime.start_generation(
                        project_id, payload, None
                    )
                else:
                    project = self.server.long_store.load(project_id)
                    readiness = compute_render_readiness(project)
                    if not readiness["ready"]:
                        raise LongFormError(
                            "；".join(item["message"] for item in readiness["blockers"][:8]),
                            str(readiness["blockers"][0]["code"]),
                        )
                    self.server.lmstudio_manager.assert_owned_unloaded()
                    result = self.server.context_runtime.start_render(project_id, payload)
                self._send_json(202, result)
                return

            reconcile_match = re.fullmatch(
                r"/api/long/projects/([A-Za-z0-9_-]+)/segments/(seg_\d{4,})/reconcile",
                path,
            )
            if reconcile_match:
                project_id, segment_id = reconcile_match.groups()
                settings = self.server.provider_settings
                result = self.server.long_runtime.start_reconcile(
                    project_id,
                    segment_id,
                    settings,
                )
                self._send_json(202, result)
                return

            reconcile_commit_match = re.fullmatch(
                r"/api/long/projects/([A-Za-z0-9_-]+)/reconcile/commit",
                path,
            )
            if reconcile_commit_match:
                updated = self.server.long_runtime.commit_reconcile_proposal(
                    str(payload.get("proposal_id") or ""),
                    accept_boundary=bool(payload.get("accept_boundary")),
                )
                if updated.get("id") != reconcile_commit_match.group(1):
                    raise LongFormError(
                        "边界确认不属于当前项目。", "content_sync_conflict"
                    )
                self._send_json(
                    200,
                    {
                        "project": updated,
                        "readiness": compute_render_readiness(updated),
                    },
                )
                return

            segment_match = re.fullmatch(
                r"/api/long/projects/([A-Za-z0-9_-]+)/segments/(seg_\d{4,})",
                path,
            )
            if segment_match:
                project = self.server.long_store.load(segment_match.group(1))
                segment_id = segment_match.group(2)
                if "story_card" in payload:
                    updated = edit_story_card(
                        self.server.long_store,
                        project,
                        segment_id,
                        payload.get("story_card") or {},
                        confirm_invalidate=bool(payload.get("confirm_invalidate")),
                    )
                elif "single_workspace" in payload:
                    workspace = deepcopy(payload.get("single_workspace") or {})
                    target = next(
                        item for item in project["segments"] if item["id"] == segment_id
                    )
                    pictures = workspace.setdefault("pictures", {})
                    for slot in ("picture1", "picture2"):
                        picture = pictures.get(slot)
                        if not isinstance(picture, dict):
                            continue
                        if picture.get("source") == "project_asset":
                            # Re-resolve the hash and replace browser-supplied
                            # metadata with the server's canonical project URL.
                            pictures[slot] = self.server.asset_store.public(
                                segment_match.group(1), picture
                            )
                    form = sanitize_form(workspace.get("form"))
                    expected_duration = int(target["frames"]) / 24
                    if abs(float(form["duration"]) - expected_duration) > 0.0005:
                        raise LongFormError(
                            "单段工作区时长必须等于剧情卡片的精确帧时长。",
                            "single_workspace_duration_mismatch",
                        )
                    form["duration"] = expected_duration
                    if target["index"] > 1 and target["boundary_before"] == "continuous":
                        if form["mode"] not in {"I2VA", "FL2VA"}:
                            raise LongFormError(
                                "连续段必须使用上一段尾帧，因此模式只能是 I2VA 或 FL2VA。",
                                "single_workspace_mode_invalid",
                            )
                        pictures.setdefault("picture1", {})["source"] = "auto_tail"
                    workspace["form"] = form
                    script = workspace.get("script")
                    normalized_script = None
                    if script is not None:
                        normalized_script = normalize_script_result(
                            {"script": script, "warnings": []}, form
                        )["script"]
                        workspace["script"] = normalized_script
                    prompt = str(workspace.get("prompt") or "").strip()
                    if prompt:
                        if normalized_script is None:
                            raise LongFormError(
                                "保存 H3 提示词前必须先生成分镜。",
                                "single_workspace_script_missing",
                            )
                        validation = validate_h3_prompt(
                            form["mode"],
                            prompt,
                            float(form["duration"]),
                            len(normalized_script["shots"]),
                            _script_dialogues(normalized_script),
                            _script_visible_text(normalized_script),
                        )
                        workspace["validation"] = validation
                        workspace["state"] = "valid" if validation["valid"] else "draft"
                    else:
                        workspace["validation"] = {
                            "valid": False,
                            "errors": [],
                            "warnings": [],
                        }
                        workspace["state"] = "draft" if normalized_script else "empty"
                    updated = save_segment_workspace(
                        self.server.long_store,
                        project,
                        segment_id,
                        workspace,
                        expected_revision=(
                            int(payload["expected_revision"])
                            if payload.get("expected_revision") is not None
                            else None
                        ),
                    )
                else:
                    updated = edit_segment(
                        self.server.long_store,
                        project,
                        segment_id,
                        payload.get("changes") or {},
                    )
                self._send_json(
                    200,
                    {
                        "project": updated,
                        "receipt": workspace_save_receipt(updated, segment_id),
                    },
                )
                return

            timeline_match = re.fullmatch(
                r"/api/long/projects/([A-Za-z0-9_-]+)/timeline", path
            )
            if timeline_match:
                project = self.server.long_store.load(timeline_match.group(1))
                updated = mutate_timeline(
                    self.server.long_store,
                    project,
                    operation=str(payload.get("operation") or ""),
                    segment_id=str(payload.get("segment_id") or ""),
                    destination_index=payload.get("destination_index"),
                )
                self._send_json(
                    200,
                    {
                        "project": updated,
                        "readiness": compute_render_readiness(updated),
                    },
                )
                return

            action_match = re.fullmatch(
                r"/api/long/projects/([A-Za-z0-9_-]+)/(regenerate|precompile|render|restore)",
                path,
            )
            if action_match:
                project_id, action = action_match.groups()
                if action == "regenerate":
                    settings = self.server.provider_settings
                    result = self.server.long_runtime.start_regeneration(
                        project_id, payload, settings
                    )
                    self._send_json(202, result)
                    return
                if action == "precompile":
                    settings = self.server.provider_settings
                    result = self.server.long_runtime.start_precompile(
                        project_id, settings
                    )
                    self._send_json(202, result)
                    return
                if action == "render":
                    self.server.lmstudio_manager.assert_owned_unloaded()
                    result = self.server.long_runtime.start_render(project_id, None)
                    self._send_json(202, result)
                    return
                revision = int(payload.get("revision") or 0)
                restored = self.server.long_store.restore(project_id, revision)
                self._send_json(
                    200,
                    {
                        "project": restored,
                        "readiness": compute_render_readiness(restored),
                    },
                )
                return
            self._send_json(
                404,
                {"error": {"code": "not_found", "message": "未找到。", "status": 404}},
            )
        except StudioError as exc:
            self._send_json(exc.status, {"error": exc.as_dict()})
        except LMStudioError as exc:
            self._send_json(
                exc.status,
                {"error": {"code": exc.code, "message": exc.message, "status": exc.status}},
            )
        except LongFormError as exc:
            status = (
                404
                if exc.code.endswith("not_found")
                else 409
                if exc.code
                in {
                    "single_workspace_conflict",
                    "story_card_invalidation_confirmation_required",
                    "precompile_already_running",
                    "content_sync_conflict",
                    "content_sync_proposal_not_found",
                }
                else 400
            )
            self._send_json(
                status,
                {"error": {"code": exc.code, "message": exc.message, "status": status}},
            )
        except (TypeError, ValueError):
            self._send_json(
                400,
                {
                    "error": {
                        "code": "request_invalid",
                        "message": "长视频请求参数无效。",
                        "status": 400,
                    }
                },
            )
        except Exception:
            self._send_json(
                500,
                {
                    "error": {
                        "code": "internal_error",
                        "message": "本地服务发生未预期错误。",
                        "status": 500,
                    }
                },
            )


def verify_runtime_files() -> None:
    required = (
        CONFIG_PATH,
        SCRIPT_PROMPT_PATH,
        VISION_PROMPT_PATH,
        COMPILER_PROMPT_PATH,
        H3_SKILL_PATH,
        H3_BASE_PATH,
        H3_REF_PATH,
        PROJECT_ROOT / "prompts" / "long_outline.md",
        PROJECT_ROOT / "prompts" / "long_segment.md",
        PROJECT_ROOT / "prompts" / "long_regenerate.md",
        PROJECT_ROOT / "prompts" / "long_compiler.md",
        PROJECT_ROOT / "prompts" / "long_qc.md",
        PROJECT_ROOT / "prompts" / "long_reconcile.md",
        PROJECT_ROOT / "context_loop.py",
        PROJECT_ROOT / "context_runtime.py",
        PROJECT_ROOT / "project_assets.py",
        PROJECT_ROOT / "lmstudio_runtime.py",
        PROJECT_ROOT / ".h3-idea2video-root",
        PROJECT_ROOT / "tools" / "build_context_workflow.py",
        PROJECT_ROOT / "comfyui_nodes" / "H3PromptStudioRuleAdapter" / "__init__.py",
        PROJECT_ROOT / "comfyui_nodes" / "H3PromptStudioRuleAdapter" / "nodes.py",
        PROJECT_ROOT / "vendor" / "minimax-h3-contex-loop" / "chain_nodes.py",
        PROJECT_ROOT / "vendor" / "minimax-h3-contex-loop" / "LICENSE",
        WEB_ROOT / "index.html",
        WEB_ROOT / "app.js",
        WEB_ROOT / "style.css",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise SystemExit("缺少运行文件：\n" + "\n".join(missing))


def create_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    runs_root: Path = RUNS_ROOT,
    config_path: Path = CONFIG_PATH,
    comfy_client_factory: Callable[[], ComfyClient] | None = None,
    provider_call: Callable[..., Any] | None = None,
    lmstudio_manager: LMStudioSessionManager | None = None,
) -> StudioHTTPServer:
    if str(host or "").lower() not in LOOPBACK_HOSTS:
        raise StudioError(
            "Studio 只能监听 127.0.0.1、localhost 或 ::1。",
            code="studio_host_not_loopback",
        )
    network = resolve_network_settings(config_path)
    settings = resolve_provider_settings(config_path=config_path)
    manager = lmstudio_manager or LMStudioSessionManager(settings=settings)
    active_provider = provider_call or manager.provider_call
    server_class = StudioIPv6HTTPServer if str(host).lower() == "::1" else StudioHTTPServer
    server = server_class((host, port), StudioHandler)
    server.config_path = config_path
    server.network_settings = network
    server.provider_settings = settings
    server.lmstudio_manager = manager
    server.provider_call = active_provider
    server.long_store = LongProjectStore(runs_root)
    server.asset_store = ProjectAssetStore(server.long_store)
    shared_render_lock = threading.Lock()
    long_comfy_factory = comfy_client_factory or (
        lambda: ComfyClient(base_url=network.comfyui_base_url)
    )
    context_comfy_factory = comfy_client_factory or (
        lambda: ContextComfyClient(base_url=network.comfyui_base_url)
    )
    server.long_runtime = LongFormRuntime(
        store=server.long_store,
        project_root=PROJECT_ROOT,
        provider_call=active_provider,
        segment_precompiler=precompile_segment_workspace,
        comfy_client_factory=long_comfy_factory,
        render_lock=shared_render_lock,
    )
    server.context_runtime = ContextLoopRuntime(
        store=server.long_store,
        project_root=PROJECT_ROOT,
        provider_call=active_provider,
        comfy_client_factory=context_comfy_factory,
        render_lock=shared_render_lock,
    )
    return server


def browser_url(host: str, port: int) -> str:
    browser_host = host
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    return f"http://{browser_host}:{port}"


def studio_is_running(
    url: str,
    *,
    urlopen_func: Callable[..., Any] = urllib.request.urlopen,
) -> bool:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/health",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen_func(request, timeout=1.5) as response:
            raw = response.read(4096)
        health = json.loads(raw.decode("utf-8"))
    except (OSError, TimeoutError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(health, dict)
        and health.get("service") == "MiniMax H3 Prompt Studio"
        and health.get("build_id") == APP_BUILD_ID
        and health.get("long_api_version") == LONG_API_VERSION
        and health.get("project_schema_version") == SCHEMA_VERSION
    )


def studio_health(
    url: str,
    *,
    urlopen_func: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any] | None:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/health",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen_func(request, timeout=1.5) as response:
            value = json.loads(response.read(4096).decode("utf-8"))
    except (OSError, TimeoutError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def open_browser(url: str, *, opener: Callable[..., Any] = webbrowser.open) -> bool:
    try:
        return bool(opener(url, new=2))
    except (OSError, webbrowser.Error):
        return False


def schedule_browser_open(
    url: str,
    *,
    delay: float = 0.35,
    opener: Callable[..., Any] = webbrowser.open,
) -> threading.Timer:
    def launch() -> None:
        if not open_browser(url, opener=opener):
            print(f"无法自动打开浏览器，请手动访问：{url}", file=sys.stderr)

    timer = threading.Timer(delay, launch)
    timer.daemon = True
    timer.start()
    return timer


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="覆盖 config.json 的 studio_port（仅本次启动）。",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="服务就绪后打开默认浏览器；若服务已运行则直接打开现有页面。",
    )
    args = parser.parse_args()
    verify_runtime_files()
    try:
        configured_network = resolve_network_settings()
    except StudioError as exc:
        print(f"配置错误：{exc.message}", file=sys.stderr)
        return 2
    active_port = args.port if args.port is not None else configured_network.studio_port
    if not 1 <= active_port <= 65535:
        print("Studio 端口必须在 1–65535 之间。", file=sys.stderr)
        return 2
    url = browser_url(args.host, active_port)
    if args.open_browser:
        existing = studio_health(url)
        if existing and existing.get("service") == "MiniMax H3 Prompt Studio":
            compatible = (
                existing.get("build_id") == APP_BUILD_ID
                and existing.get("long_api_version") == LONG_API_VERSION
                and existing.get("project_schema_version") == SCHEMA_VERSION
            )
            if not compatible:
                print(
                    f"检测到旧版服务正占用 {active_port}；请先关闭旧服务再启动。"
                    f" 当前 build={existing.get('build_id') or 'legacy'}，需要 {APP_BUILD_ID}。",
                    file=sys.stderr,
                )
                return 2
            if not open_browser(url):
                print(f"无法自动打开浏览器，请手动访问：{url}", file=sys.stderr)
                return 1
            print(f"服务已在运行，已打开：{url}")
            return 0
    try:
        server = create_server(args.host, active_port)
    except StudioError as exc:
        print(f"启动失败：{exc.message}", file=sys.stderr)
        return 2
    print(f"MiniMax H3 Idea2Video: {url}")
    print(
        "Provider: LM Studio / qwen3.6-27b-uncensored-hauhaucs-aggressive "
        "(only loaded after entering edit mode or an AI action)"
    )
    if args.open_browser:
        schedule_browser_open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
