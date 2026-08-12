#!/usr/bin/env python3
"""LM Studio lifecycle and native REST streaming provider.

This module owns exactly one explicitly named LM Studio model instance.  It
never unloads arbitrary instances and never stops the LM Studio local server.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator


DEFAULT_BASE_URL = "http://127.0.0.1:1234/api/v1"
DEFAULT_MODEL = "qwen3.6-27b-uncensored-hauhaucs-aggressive"
DEFAULT_IDENTIFIER = "h3-script-editor"
DEFAULT_CONTEXT_LENGTH = 131_072
DEFAULT_SERVER_PORT = 1234
PROVIDER_TIMEOUT_SECONDS = 600


class LMStudioError(Exception):
    """Expected LM Studio error safe to return to the local browser."""

    def __init__(self, message: str, code: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


@dataclass(frozen=True)
class LMStudioSettings:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    identifier: str = DEFAULT_IDENTIFIER
    stream: bool = True
    reasoning: str = "on"
    auto_start_server: bool = True


def _json_object(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    candidates = [value]
    first, last = value.find("{"), value.rfind("}")
    if first >= 0 and last > first:
        candidates.append(value[first : last + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise LMStudioError(
        "本地模型返回的内容不是有效 JSON 对象，请重试。",
        "provider_json_invalid",
        502,
    )


def _merge_usage(target: dict[str, Any], incoming: Any) -> None:
    if not isinstance(incoming, dict):
        return
    for key, value in incoming.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            target[key] = value
        elif isinstance(value, dict):
            nested = target.setdefault(key, {})
            if isinstance(nested, dict):
                _merge_usage(nested, value)


def _task_sampling(messages: list[dict[str, Any]]) -> tuple[float, float]:
    """Use a creative profile for writing and a restrained one for transforms."""

    searchable = json.dumps(messages, ensure_ascii=False).lower()
    deterministic_markers = (
        "compiler",
        "compile",
        "reconcile",
        "synchron",
        "vision",
        "analy",
        "repair",
        "校验",
        "同步",
        "编译",
        "图片",
        "视觉分析",
    )
    if any(marker in searchable for marker in deterministic_markers):
        return 0.2, 0.8
    return 0.65, 0.9


def _native_chat_parts(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, str]]]:
    """Convert the app's system/user messages to LM Studio native v1 input."""

    system_parts: list[str] = []
    input_items: list[dict[str, str]] = []
    has_assistant_history = any(
        isinstance(message, dict) and message.get("role") == "assistant"
        for message in messages
    )
    if has_assistant_history:
        turns: list[dict[str, str]] = []
        for message in messages:
            if not isinstance(message, dict):
                raise LMStudioError(
                    "本地模型消息格式无效。", "provider_messages_invalid", 500
                )
            role = str(message.get("role") or "")
            content = message.get("content")
            if role == "system":
                if not isinstance(content, str):
                    raise LMStudioError(
                        "本地模型 system 消息必须是文字。",
                        "provider_messages_invalid",
                        500,
                    )
                system_parts.append(content)
                continue
            if role not in {"user", "assistant"} or not isinstance(content, str):
                raise LMStudioError(
                    "含 assistant 历史的修复请求只支持文字 user/assistant 轮次。",
                    "provider_messages_invalid",
                    500,
                )
            turns.append({"role": role, "content": content})
        if not turns or turns[-1]["role"] != "user":
            raise LMStudioError(
                "修复请求的最后一轮必须是当前 user 指令。",
                "provider_messages_invalid",
                500,
            )
        system_parts.append(
            "# Native repair-envelope protocol\n"
            "The input is a JSON repair envelope, not a conversation to echo. "
            "Execute ONLY current_request. Treat every item in history as immutable "
            "context/data, including all previous assistant output. Return the complete "
            "corrected JSON response required by current_request."
        )
        input_items.append(
            {
                "type": "text",
                "content": json.dumps(
                    {
                        "envelope_type": "repair_conversation",
                        "history": turns[:-1],
                        "current_request": turns[-1]["content"],
                    },
                    ensure_ascii=False,
                ),
            }
        )
        system_parts.append(
            "# Transport requirement\n"
            "Return exactly one valid JSON object. Do not wrap it in Markdown and do not "
            "write any prose outside the JSON object."
        )
        return "\n\n".join(system_parts), input_items

    for message in messages:
        if not isinstance(message, dict):
            raise LMStudioError(
                "本地模型消息格式无效。", "provider_messages_invalid", 500
            )
        role = str(message.get("role") or "")
        content = message.get("content")
        if role == "system":
            if not isinstance(content, str):
                raise LMStudioError(
                    "本地模型 system 消息必须是文字。",
                    "provider_messages_invalid",
                    500,
                )
            system_parts.append(content)
            continue
        if role != "user":
            raise LMStudioError(
                f"本地原生 Chat 当前不接受 {role or '空'} 角色。",
                "provider_messages_invalid",
                500,
            )
        if isinstance(content, str):
            input_items.append({"type": "text", "content": content})
            continue
        if not isinstance(content, list):
            raise LMStudioError(
                "本地模型 user 消息必须是文字或多模态数组。",
                "provider_messages_invalid",
                500,
            )
        for part in content:
            if not isinstance(part, dict):
                raise LMStudioError(
                    "本地模型多模态消息条目无效。",
                    "provider_messages_invalid",
                    500,
                )
            part_type = str(part.get("type") or "")
            if part_type in {"text", "message"}:
                value = part.get("text") if part_type == "text" else part.get("content")
                input_items.append({"type": "text", "content": str(value or "")})
                continue
            if part_type in {"image_url", "image"}:
                value: Any = part.get("image_url") if part_type == "image_url" else part.get("data_url")
                if isinstance(value, dict):
                    value = value.get("url")
                data_url = str(value or "")
                if not data_url.startswith("data:image/"):
                    raise LMStudioError(
                        "本地图片输入必须是 data:image/... URL。",
                        "provider_messages_invalid",
                        500,
                    )
                input_items.append({"type": "image", "data_url": data_url})
                continue
            raise LMStudioError(
                f"本地模型不支持多模态条目类型：{part_type or '空'}。",
                "provider_messages_invalid",
                500,
            )
    if not input_items:
        raise LMStudioError(
            "本地模型请求缺少 user 输入。", "provider_messages_invalid", 500
        )
    system_parts.append(
        "# Transport requirement\n"
        "Return exactly one valid JSON object. Do not wrap it in Markdown and do not "
        "write any prose outside the JSON object."
    )
    return "\n\n".join(system_parts), input_items


def _iter_sse_events(response: Any) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield named SSE events, including a final block without a blank line."""

    event_name = "message"
    data_lines: list[str] = []

    def decode() -> tuple[str, dict[str, Any]] | None:
        if not data_lines:
            return None
        raw = "\n".join(data_lines)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LMStudioError(
                "LM Studio 原生流包含无效 JSON 数据块。",
                "provider_stream_invalid",
                502,
            ) from exc
        if not isinstance(value, dict):
            raise LMStudioError(
                "LM Studio 原生流事件必须是 JSON 对象。",
                "provider_stream_invalid",
                502,
            )
        return event_name, value

    while True:
        raw_line = response.readline()
        if not raw_line:
            final = decode()
            if final is not None:
                yield final
            return
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            event = decode()
            if event is not None:
                yield event
            event_name = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())


class LMStudioSessionManager:
    """Own and serve one fixed LM Studio model instance."""

    def __init__(
        self,
        *,
        settings: LMStudioSettings | None = None,
        cli_path: str | Path | None = None,
        command_runner: Callable[..., Any] | None = None,
        urlopen_func: Callable[..., Any] | None = None,
        port_in_use_func: Callable[[str, int], bool] | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings or LMStudioSettings()
        self._cli_path = str(cli_path) if cli_path else ""
        self._command_runner = command_runner or subprocess.run
        self._urlopen = urlopen_func or urllib.request.urlopen
        self._port_in_use_func = port_in_use_func or self._tcp_port_in_use
        self._sleep = sleep_func
        self._lock = threading.RLock()
        self._active_requests = 0
        self._last_error: dict[str, Any] | None = None

    @staticmethod
    def _tcp_port_in_use(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False

    def _find_cli(self) -> str:
        if self._cli_path:
            if Path(self._cli_path).is_file():
                return self._cli_path
            raise LMStudioError(
                f"找不到 LM Studio CLI：{self._cli_path}",
                "lmstudio_cli_missing",
                503,
            )
        found = shutil.which("lms")
        if found:
            self._cli_path = found
            return found
        home = Path(os.environ.get("USERPROFILE") or Path.home())
        candidate = home / ".lmstudio" / "bin" / "lms.exe"
        if candidate.is_file():
            self._cli_path = str(candidate)
            return self._cli_path
        raise LMStudioError(
            "找不到 LM Studio CLI（lms.exe）。请先安装并启动 LM Studio。",
            "lmstudio_cli_missing",
            503,
        )

    def _run(self, args: list[str], *, timeout: int = 180) -> Any:
        cli = self._find_cli()
        kwargs: dict[str, Any] = {
            "args": [cli, *args],
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout,
            "check": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = self._command_runner(**kwargs)
        except subprocess.TimeoutExpired as exc:
            raise LMStudioError(
                f"LM Studio 命令超时：{' '.join(args[:2])}",
                "lmstudio_cli_timeout",
                504,
            ) from exc
        except OSError as exc:
            raise LMStudioError(
                f"无法执行 LM Studio CLI：{exc}",
                "lmstudio_cli_failed",
                503,
            ) from exc
        if int(getattr(result, "returncode", 1)) != 0:
            detail = str(getattr(result, "stderr", "") or getattr(result, "stdout", "")).strip()
            if len(detail) > 800:
                detail = detail[:800] + "…"
            raise LMStudioError(
                f"LM Studio 命令失败：{detail or '未知错误'}",
                "lmstudio_cli_failed",
                503,
            )
        return result

    def _json_command(self, args: list[str]) -> list[dict[str, Any]]:
        result = self._run(args)
        try:
            value = json.loads(str(getattr(result, "stdout", "") or "[]"))
        except json.JSONDecodeError as exc:
            raise LMStudioError(
                "LM Studio CLI 返回了无法解析的状态。",
                "lmstudio_cli_response_invalid",
                502,
            ) from exc
        if isinstance(value, dict):
            for key in ("models", "instances", "loadedModels", "data"):
                if isinstance(value.get(key), list):
                    value = value[key]
                    break
        if not isinstance(value, list):
            raise LMStudioError(
                "LM Studio CLI 状态格式不受支持。",
                "lmstudio_cli_response_invalid",
                502,
            )
        return [item for item in value if isinstance(item, dict)]

    @staticmethod
    def _instance_identifier(item: dict[str, Any]) -> str:
        for key in ("identifier", "instanceIdentifier", "id", "instance_id"):
            if item.get(key):
                return str(item[key])
        return ""

    @staticmethod
    def _instance_model(item: dict[str, Any]) -> str:
        for key in (
            "modelKey",
            "model",
            "model_key",
            "modelPath",
            "path",
            "indexedModelIdentifier",
        ):
            value = item.get(key)
            if isinstance(value, dict):
                value = value.get("modelKey") or value.get("path") or value.get("key")
            if value:
                return str(value)
        return ""

    def _same_target_model(self, value: str) -> bool:
        lowered = value.replace("\\", "/").lower()
        target = self.settings.model.lower()
        return lowered == target or target in lowered

    def _installed_models(self) -> list[dict[str, Any]]:
        return self._json_command(["ls", "--json"])

    def _loaded_instances(self) -> list[dict[str, Any]]:
        return self._json_command(["ps", "--json"])

    def _server_running(self) -> bool:
        url = self.settings.base_url.rstrip("/") + "/models"
        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        try:
            with self._urlopen(request, timeout=2) as response:
                status = int(getattr(response, "status", 200))
                if status >= 400:
                    return False
                raw = response.read(1_000_000)
                parsed = json.loads(raw.decode("utf-8"))
                return isinstance(parsed, (dict, list))
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, TimeoutError):
            return False

    def _server_bind(self) -> tuple[str, int]:
        parsed = urllib.parse.urlparse(self.settings.base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port is None
            or parsed.path.rstrip("/") != "/api/v1"
        ):
            raise LMStudioError(
                "LM Studio 地址必须是本机 http://127.0.0.1:<端口>/api/v1。",
                "lmstudio_base_url_invalid",
                500,
            )
        return "127.0.0.1", parsed.port

    def _snapshot(self) -> dict[str, Any]:
        cli = self._find_cli()
        installed = self._installed_models()
        loaded = self._loaded_instances()
        target_installed = any(
            self._same_target_model(str(item.get("modelKey") or item.get("path") or ""))
            for item in installed
        )
        own: dict[str, Any] | None = None
        conflicts: list[dict[str, str]] = []
        identifier_conflict: dict[str, str] | None = None
        for item in loaded:
            identifier = self._instance_identifier(item)
            model = self._instance_model(item)
            if identifier == self.settings.identifier:
                if self._same_target_model(model):
                    own = item
                else:
                    identifier_conflict = {"identifier": identifier, "model": model}
            elif self._same_target_model(model):
                conflicts.append({"identifier": identifier or "未命名实例", "model": model})
        return {
            "cli": {"available": True, "path": cli},
            "server": {"running": self._server_running(), "base_url": self.settings.base_url},
            "model": {
                "key": self.settings.model,
                "identifier": self.settings.identifier,
                "installed": target_installed,
                "owned_loaded": own is not None,
                "external_conflicts": conflicts,
                "identifier_conflict": identifier_conflict,
                "context_length": DEFAULT_CONTEXT_LENGTH,
            },
            "active_requests": self._active_requests,
            "last_error": self._last_error,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            try:
                cli = self._find_cli()
                if not self._server_running():
                    # A passive page load must never wake LM Studio or block on
                    # its CLI bootstrap. The explicit Start action performs the
                    # full installed/loaded/conflict inspection.
                    self._last_error = None
                    return {
                        "cli": {"available": True, "path": cli},
                        "server": {"running": False, "base_url": self.settings.base_url},
                        "model": {
                            "key": self.settings.model,
                            "identifier": self.settings.identifier,
                            "installed": None,
                            "owned_loaded": False,
                            "external_conflicts": [],
                            "identifier_conflict": None,
                            "context_length": DEFAULT_CONTEXT_LENGTH,
                        },
                        "active_requests": self._active_requests,
                        "last_error": None,
                    }
                result = self._snapshot()
                self._last_error = None
                return result
            except LMStudioError as exc:
                self._last_error = {"code": exc.code, "message": exc.message}
                return {
                    "cli": {"available": False, "path": self._cli_path},
                    "server": {"running": False, "base_url": self.settings.base_url},
                    "model": {
                        "key": self.settings.model,
                        "identifier": self.settings.identifier,
                        "installed": False,
                        "owned_loaded": False,
                        "external_conflicts": [],
                        "identifier_conflict": None,
                        "context_length": DEFAULT_CONTEXT_LENGTH,
                    },
                    "active_requests": self._active_requests,
                    "last_error": self._last_error,
                }

    def ensure_server(self) -> dict[str, Any]:
        with self._lock:
            self._find_cli()
            if self._server_running():
                return self.status()
            if not self.settings.auto_start_server:
                raise LMStudioError(
                    f"LM Studio API 未在 {self.settings.base_url} 运行，且自动启动已关闭。"
                    "请先手动执行 lms server start，或在端口设置中开启自动启动。",
                    "lmstudio_server_not_running",
                    503,
                )
            bind_host, server_port = self._server_bind()
            if self._port_in_use_func(bind_host, server_port):
                raise LMStudioError(
                    f"端口 {server_port} 已被其他程序占用，但它不是可用的 LM Studio API。"
                    "请在页面的本地端口设置中选择另一个端口并重启 Prompt Studio。",
                    "lmstudio_port_occupied",
                    409,
                )
            self._run(
                [
                    "server",
                    "start",
                    "--port",
                    str(server_port),
                    "--bind",
                    bind_host,
                ],
                timeout=180,
            )
            for _ in range(120):
                if self._server_running():
                    break
                self._sleep(0.25)
            if not self._server_running():
                raise LMStudioError(
                    "LM Studio 本地 API 服务启动后仍不可访问。",
                    "lmstudio_server_unavailable",
                    503,
                )
            return self.status()

    def ensure_ready(self) -> dict[str, Any]:
        with self._lock:
            self.ensure_server()
            snapshot = self._snapshot()
            model = snapshot["model"]
            if not model["installed"]:
                raise LMStudioError(
                    f"LM Studio 中未找到模型：{self.settings.model}",
                    "lmstudio_model_missing",
                    503,
                )
            if model["identifier_conflict"]:
                other = model["identifier_conflict"]
                raise LMStudioError(
                    f"实例名 {self.settings.identifier} 已被其他模型占用：{other['model']}。",
                    "lmstudio_identifier_conflict",
                    409,
                )
            if model["external_conflicts"]:
                names = "、".join(item["identifier"] for item in model["external_conflicts"])
                raise LMStudioError(
                    "目标模型已被 Chatbox 或其他 LM Studio 会话加载"
                    f"（{names}）。请先在对应应用中释放它，再进入本项目编辑模式。",
                    "lmstudio_model_conflict",
                    409,
                )
            if not model["owned_loaded"]:
                self._run(
                    [
                        "load",
                        self.settings.model,
                        "--gpu",
                        "max",
                        "--context-length",
                        str(DEFAULT_CONTEXT_LENGTH),
                        "--parallel",
                        "1",
                        "--identifier",
                        self.settings.identifier,
                        "-y",
                    ],
                    timeout=600,
                )
            verified = self._snapshot()
            if not verified["model"]["owned_loaded"]:
                raise LMStudioError(
                    "模型加载命令已完成，但未发现项目专属实例。",
                    "lmstudio_load_not_verified",
                    503,
                )
            self._last_error = None
            return verified

    @contextmanager
    def request_session(self) -> Iterator[None]:
        with self._lock:
            self.ensure_ready()
            self._active_requests += 1
        try:
            yield
        finally:
            with self._lock:
                self._active_requests = max(0, self._active_requests - 1)

    def release(self) -> dict[str, Any]:
        with self._lock:
            if self._active_requests:
                raise LMStudioError(
                    f"仍有 {self._active_requests} 个本地模型请求正在运行，暂不能释放显存。",
                    "lmstudio_requests_active",
                    409,
                )
            snapshot = self._snapshot()
            if snapshot["model"]["owned_loaded"]:
                self._run(["unload", self.settings.identifier], timeout=180)
            verified = self._snapshot()
            if verified["model"]["owned_loaded"]:
                raise LMStudioError(
                    "项目专属模型实例卸载后仍然可见，请在 LM Studio 中检查。",
                    "lmstudio_unload_not_verified",
                    503,
                )
            self._last_error = None
            return verified

    def assert_owned_unloaded(self) -> dict[str, Any]:
        """Fail closed unless this project's fixed model instance is absent."""

        with self._lock:
            snapshot = self._snapshot()
            if snapshot["model"]["owned_loaded"]:
                raise LMStudioError(
                    "项目专属本地模型仍占用显存。请先确认全部 H3 提示词并释放显存，"
                    "再启动视频生成。",
                    "lmstudio_model_still_loaded",
                    409,
                )
            return snapshot

    def provider_call(
        self,
        messages: list[dict[str, Any]],
        settings: LMStudioSettings | None = None,
        *,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
        urlopen_func: Callable[..., Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], str | None]:
        active = settings or self.settings
        temperature, top_p = _task_sampling(messages)
        system_prompt, native_input = _native_chat_parts(messages)
        payload = {
            "model": active.identifier,
            "system_prompt": system_prompt,
            "input": native_input,
            "stream": True,
            "store": False,
            "temperature": temperature,
            "top_p": top_p,
            "reasoning": active.reasoning,
        }
        request = urllib.request.Request(
            f"{active.base_url.rstrip('/')}/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "text/event-stream",
            },
        )
        opener = urlopen_func or self._urlopen
        content_parts: list[str] = []
        usage: dict[str, Any] = {}
        finish_reason: str | None = None
        saw_chat_end = False
        with self.request_session():
            try:
                with opener(request, timeout=PROVIDER_TIMEOUT_SECONDS) as response:
                    for event_name, chunk in _iter_sse_events(response):
                        event_type = str(chunk.get("type") or event_name)
                        if event_type == "reasoning.delta":
                            reasoning = chunk.get("content")
                            if isinstance(reasoning, str) and reasoning and event_callback:
                                event_callback(
                                    "thinking",
                                    {
                                        "message": "本地 Qwen 正在思考…",
                                        "text": reasoning,
                                    },
                                )
                            continue
                        if event_type == "message.delta":
                            text = chunk.get("content")
                            if isinstance(text, str) and text:
                                content_parts.append(text)
                                if event_callback:
                                    event_callback("delta", {"text": text})
                            continue
                        if event_type == "error":
                            error = chunk.get("error")
                            if not isinstance(error, dict):
                                error = {}
                            message = str(error.get("message") or "LM Studio 原生流返回错误。")
                            code = str(error.get("code") or error.get("type") or "unknown")
                            raise LMStudioError(
                                message,
                                f"provider_stream_{code}",
                                502,
                            )
                        if event_type != "chat.end":
                            continue
                        saw_chat_end = True
                        finish_reason = "stop"
                        result = chunk.get("result")
                        if not isinstance(result, dict):
                            raise LMStudioError(
                                "LM Studio chat.end 缺少 result。",
                                "provider_stream_invalid",
                                502,
                            )
                        _merge_usage(usage, result.get("stats"))
                        if content_parts:
                            continue
                        # Normally content arrives via message.delta. Keep the
                        # aggregated chat.end result as a compatibility fallback.
                        output = result.get("output")
                        if isinstance(output, list):
                            for item in output:
                                if not isinstance(item, dict) or item.get("type") != "message":
                                    continue
                                text = item.get("content")
                                if isinstance(text, str) and text:
                                    content_parts.append(text)
                                    if event_callback:
                                        event_callback("delta", {"text": text})
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read(4096).decode("utf-8", errors="replace")
                except Exception:
                    detail = ""
                if len(detail) > 500:
                    detail = detail[:500] + "…"
                raise LMStudioError(
                    f"LM Studio API 请求失败（HTTP {exc.code}）{('：' + detail) if detail else ''}",
                    f"provider_http_{exc.code}",
                    int(exc.code),
                ) from exc
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
                raise LMStudioError(
                    "LM Studio 本地推理超时或连接中断，请确认服务和模型状态后重试。",
                    "provider_timeout",
                    504,
                ) from exc
        if not saw_chat_end:
            raise LMStudioError(
                "LM Studio 原生流在 chat.end 前中断，未采用不完整结果。",
                "provider_stream_incomplete",
                502,
            )
        content = "".join(content_parts)
        if not content.strip():
            raise LMStudioError(
                "本地模型返回了空内容，请重试。",
                "provider_content_empty",
                502,
            )
        return _json_object(content), usage, finish_reason
