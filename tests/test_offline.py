#!/usr/bin/env python3
"""Offline tests for MiniMax H3 Prompt Studio.

No test in this module performs a real provider request or a GPU operation.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import nullcontext
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import app  # noqa: E402
import build_workflows  # noqa: E402
import context_loop  # noqa: E402
import context_runtime  # noqa: E402
import longform  # noqa: E402
import longform_runtime  # noqa: E402
import lmstudio_runtime  # noqa: E402
import project_assets  # noqa: E402
from tools import build_context_workflow  # noqa: E402
from tools import build_long_workflow  # noqa: E402

try:
    from comfyui_nodes.H3PromptStudioRuleAdapter import nodes as rule_nodes  # noqa: E402
except ModuleNotFoundError as exc:  # A plain authoring Python does not include ComfyUI/Torch.
    if exc.name != "torch":
        raise
    rule_nodes = None


HAS_COMFY_TORCH = rule_nodes is not None


def artifact_receipt(project_root: Path, paths: list[Path], kind: str) -> str:
    return json.dumps(
        {
            "schema": "h3_idea2video_artifact_receipt_v1",
            "kind": kind,
            "files": [
                {
                    "relative_path": path.resolve().relative_to(
                        project_root.resolve()
                    ).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in paths
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def base_form(mode: str = "T2VA") -> dict[str, object]:
    return {
        "mode": mode,
        "duration": 7,
        "aspect_ratio": "9:16",
        "creative_brief": "一件普通物体在桌面完成一次清晰动作。",
        "visual_style": "写实电影",
        "subjects": "一个主体",
        "scene_lighting": "柔和侧光",
        "action_timeline": "主体从静止到完成动作",
        "camera_motion": "缓慢推进",
        "exact_dialogue": "你好，世界！",
        "visible_text": "营业中",
        "ambient_sound": "室内环境音",
        "music": "无",
        "extra_constraints": "保持连续",
        "picture1_description": "首帧显示主体位于画面中央" if mode != "T2VA" else "",
        "picture2_description": "尾帧显示动作完成" if mode == "FL2VA" else "",
    }


def script_result() -> dict[str, object]:
    return {
        "script": {
            "title": "通用测试",
            "logline": "主体完成一次动作。",
            "duration": 7,
            "aspect_ratio": "9:16",
            "shots": [
                {
                    "shot": 1,
                    "start": 0,
                    "end": 7,
                    "visual": "主体位于画面中央。",
                    "action": "主体完成动作。",
                    "camera": "镜头缓慢推进。",
                    "dialogue": [{"language": "Chinese", "text": "你好，世界！"}],
                    "visible_text": ["营业中"],
                    "sound": "轻微室内声。",
                    "music": "无。",
                }
            ],
        },
        "warnings": [],
    }


def valid_prompt(mode: str) -> str:
    instruction = ""
    picture_body = ""
    if mode == "I2VA":
        instruction = (
            "For the target video, at 0.00 seconds into the target video, "
            "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
        )
        picture_body = " Beginning from <Picture 1>,"
    elif mode == "FL2VA":
        instruction = (
            "How the reference pictures align with the target video — Picture 1 "
            "(from Shot 1) aligns with the 0.00-second mark of the target video; "
            "Picture 2 (from Shot 1) aligns with the 7.00-second mark of the target video.\n\n"
        )
        picture_body = " Beginning from Picture 1, the subject moves continuously and reaches Picture 2 at the end."
    return (
        instruction
        + "integrated_multimodal_description: [Shot 1] Live-action, cinematic."
        + picture_body
        + " A speaker (S1) says: <d>[Chinese] 你好，世界！</d> "
        + 'A sign reading "营业中" remains visible.\n\n'
        + "overall_soundscape: Soft indoor room tone and one subtle movement sound.\n\n"
        + "non_diegetic_music: N/A"
    )


def dynamic_script_result(form: dict[str, object]) -> dict[str, object]:
    duration = float(form["duration"])
    dialogue = [
        {"language": app.detect_language(line), "text": line}
        for line in str(form.get("exact_dialogue") or "").splitlines()
        if line.strip()
    ]
    visible_text = [
        line
        for line in str(form.get("visible_text") or "").splitlines()
        if line.strip()
    ]
    return {
        "script": {
            "title": "离线原子保存测试",
            "logline": "主体在一个连续镜头中完成本段剧情。",
            "duration": duration,
            "aspect_ratio": str(form.get("aspect_ratio") or "9:16"),
            "shots": [
                {
                    "shot": 1,
                    "start": 0,
                    "end": duration,
                    "visual": "主体和环境保持连续一致。",
                    "action": "主体完成本段计划动作并落到明确结束状态。",
                    "camera": "稳定的连续镜头。",
                    "dialogue": dialogue,
                    "visible_text": visible_text,
                    "sound": "连续环境声。",
                    "music": "N/A",
                }
            ],
        },
        "warnings": [],
    }


def dynamic_valid_prompt(form: dict[str, object], shot_count: int = 1) -> str:
    mode = str(form["mode"])
    duration = float(form["duration"])
    instruction = ""
    picture_body = ""
    if mode == "I2VA":
        instruction = (
            "For the target video, at 0.00 seconds into the target video, "
            "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
        )
        picture_body = " Beginning from <Picture 1>,"
    elif mode == "FL2VA":
        instruction = (
            "How the reference pictures align with the target video — Picture 1 "
            "(from Shot 1) aligns with the 0.00-second mark of the target video; "
            f"Picture 2 (from Shot {shot_count}) aligns with the {duration:.2f}-second "
            "mark of the target video.\n\n"
        )
        picture_body = " Beginning from Picture 1, the action reaches Picture 2 at the end."
    dialogue = "".join(
        f" A speaker says: <d>[{app.detect_language(line)}] {line}</d>"
        for line in str(form.get("exact_dialogue") or "").splitlines()
        if line.strip()
    )
    visible = "".join(
        f' A sign reading "{line}" remains visible.'
        for line in str(form.get("visible_text") or "").splitlines()
        if line.strip()
    )
    return (
        instruction
        + "integrated_multimodal_description: [Shot 1] Live-action continuous scene."
        + picture_body
        + dialogue
        + visible
        + "\n\noverall_soundscape: Continuous natural ambience."
        + "\n\nnon_diegetic_music: N/A"
    )


def fake_single_stage_provider(messages, _settings, event_callback=None):
    try:
        payload = json.loads(messages[-1]["content"])
        if isinstance(payload.get("input"), dict) and "exact_dialogue_lines" in payload:
            if event_callback:
                event_callback("delta", {"text": '{"script":'})
            return dynamic_script_result(payload["input"]), {"total_tokens": 11}, "stop"
        if isinstance(payload.get("edited_script"), dict) and "mode" in payload:
            form = {
                "mode": payload["mode"],
                "duration": payload["duration"],
                "aspect_ratio": payload["aspect_ratio"],
                "exact_dialogue": "\n".join(payload.get("original_exact_dialogue_lines") or []),
                "visible_text": "\n".join(payload.get("original_visible_text_lines") or []),
            }
            return {
                "mode": payload["mode"],
                "prompt": dynamic_valid_prompt(form, len(payload["edited_script"]["shots"])),
                "warnings": [],
            }, {"total_tokens": 13}, "stop"
        raise AssertionError("unexpected single-stage provider request")
    except Exception as exc:
        raise app.StudioError(
            f"fake provider failed: {type(exc).__name__}: {exc}",
            code="fake_provider_failed",
        ) from exc


def make_story_project(project_id: str = "atomic_project", seconds: float = 17) -> dict[str, object]:
    outline = longform.normalize_outline_result(long_outline(seconds), idea="离线原子保存")
    project = longform.make_project(
        outline,
        longform.allocate_segment_frames(seconds),
        project_id=project_id,
    )
    previous_state = ""
    for index, placeholder in enumerate(project["segments"], start=1):
        card = longform.normalize_story_card_result(
            long_story_card(
                index,
                story_target=placeholder["story_target"],
            ),
            index=index,
            story_target=placeholder["story_target"],
            previous_state=previous_state,
        )
        project["segments"][index - 1] = longform.apply_story_card(placeholder, card)
        previous_state = card["ending_state"]
    return project


def context_prompt(
    *,
    scene_index: int,
    identity_count: int = 0,
    opening: bool = False,
    dialogue: list[dict[str, str]] | None = None,
    visible_text: list[str] | None = None,
) -> str:
    """Create a compact valid H3 fixture for deterministic rule tests."""

    dialogue_text = "".join(
        f" A speaker says <d>[{item['language']}] {item['text']}</d>."
        for item in (dialogue or [])
    )
    visible = "".join(
        f' The visible words "{item}" remain exactly unchanged.'
        for item in (visible_text or [])
    )
    if identity_count:
        picture_count = identity_count + (1 if opening and scene_index == 1 else 0)
        definitions = [
            f"<Subject {number}> is the consistent subject defined by <Picture {number}>."
            for number in range(1, identity_count + 1)
        ]
        definitions.extend(
            f"<Picture {number}> is connected reference image {number}."
            for number in range(1, picture_count + 1)
        )
        refs = ", ".join(f"<Picture {number}>" for number in range(1, picture_count + 1))
        return (
            "subject_definitions:\n"
            + "\n".join(definitions)
            + "\n\nsummary:\nA continuous cinematic scene.\n\n"
            + "retention_analysis:\nRetain identity and wardrobe from "
            + refs
            + ".\n\n"
            + "detailed_description:\n[Shot 1] The action advances naturally in one take."
            + dialogue_text
            + visible
            + "\n\noverall_soundscape:\nNatural synchronized ambience."
            + "\n\nnon_diegetic_music:\nN/A"
        )
    prefix = context_loop.I2VA_LINE + "\n\n" if opening and scene_index == 1 else ""
    return (
        prefix
        + "integrated_multimodal_description:\n"
        + "[Shot 1] The action advances naturally in one continuous cinematic take."
        + dialogue_text
        + visible
        + "\n\noverall_soundscape:\nNatural synchronized ambience."
        + "\n\nnon_diegetic_music:\nN/A"
    )


def make_context_spec(
    project: dict[str, object],
    *,
    opening: bool = False,
    identity_count: int = 0,
) -> dict[str, object]:
    if identity_count:
        raise ValueError("rule-loop fixtures do not create a second Ref2VA layer")
    for index, segment in enumerate(project["segments"], start=1):
        workspace = segment["single_workspace"]
        form = app.sanitize_form(workspace["form"])
        if index == 1 and opening:
            form["mode"] = "I2VA"
            form["picture1_description"] = "Saved opening frame."
            workspace["pictures"]["picture1"] = {
                "source": "input",
                "input_path": "opening.png",
                "temporary_name": "",
            }
        script = app.normalize_script_result(dynamic_script_result(form), form)["script"]
        workspace.update(
            {
                "form": form,
                "script": script,
                "prompt": dynamic_valid_prompt(form),
                "validation": {"valid": True, "errors": [], "warnings": []},
                "state": "valid",
            }
        )
        longform._materialize_workspace(segment)
        segment["content_sync"] = longform.make_content_sync(
            segment, state="clean", source="rule_fixture"
        )
    return context_loop.build_rule_spec(project, base_seed=17)


def long_outline(seconds: float = 58) -> dict[str, object]:
    return {
        "project": {
            "title": "离线长视频测试",
            "language": "Chinese",
            "suggested_total_seconds": seconds,
            "story_bible": {
                "premise": "一个主体完成完整旅程。",
                "characters": [{"name": "甲", "identity": "固定外观"}],
                "world": "通用测试空间",
                "visual_rules": ["保持身份一致"],
                "continuity_rules": ["承接上一段尾帧"],
                "audio_rules": ["保留模型原生音频"],
            },
            "outline": [
                {"chapter": 1, "title": "开始", "summary": "建立目标", "turning_point": "出发"},
                {"chapter": 2, "title": "结束", "summary": "完成目标", "turning_point": "抵达"},
            ],
            "ending_requirements": ["故事完整结束"],
        },
        "warnings": [],
    }


def long_segment(
    index: int,
    *,
    character: bool = False,
    frames: int = 168,
    story_target: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "segment": {
            "boundary_before": "start" if index == 1 else "continuous",
            "summary": f"第 {index} 段推进剧情",
            "visual": f"通用场景中的第 {index} 段画面",
            "beats": [
                {
                    "start_seconds": "0.000",
                    "end_seconds": longform.canonical_beat_seconds(frames),
                    "action": f"主体在本段完成动作 {index}",
                }
            ],
            "camera": "稳定跟拍",
            "dialogue": [],
            "visible_text": [],
            "sound": "连续环境声",
            "music": "N/A",
            "present_characters": ["甲"] if character else [],
            "covered_outline_chapters": list(
                (story_target or {}).get("chapter_numbers") or []
            ),
            "fulfilled_ending_requirements": list(
                (story_target or {}).get("required_ending_conditions") or []
            ),
            "continuity_in": f"状态 {index - 1}",
            "continuity_out": f"状态 {index}",
            "extra_constraints": "",
        },
        "warnings": [],
    }


def long_story_card(
    index: int,
    *,
    story_target: dict[str, object],
    dialogue: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "segment": {
            "title": f"剧情卡 {index}",
            "story_text": f"第 {index} 段中文剧情只描述故事推进，不预写 H3 分镜。",
            "dialogue": dialogue or [],
            "opening_state": f"开场状态 {index}",
            "ending_state": f"结尾状态 {index}",
            "present_characters": ["甲"],
            "boundary_before": "start" if index == 1 else "continuous",
            "covered_outline_chapters": list(story_target.get("chapter_numbers") or []),
            "fulfilled_ending_requirements": list(
                story_target.get("required_ending_conditions") or []
            ),
        },
        "warnings": [],
    }


def wait_background_task(runtime: longform_runtime.LongFormRuntime, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        task = runtime.task(task_id)
        if task["state"] not in {"queued", "running", "retrying"} and task.get("finished_at"):
            return task
        time.sleep(0.01)
    raise AssertionError("background task did not finish")


def parse_local_sse(raw: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        name = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            events.append((name, json.loads("\n".join(data_lines))))
    return events


class FakeSSEBody:
    def __init__(self, lines: list[bytes], status: int = 200) -> None:
        physical_lines: list[bytes] = []
        for value in lines:
            physical_lines.extend(value.splitlines(keepends=True))
        self.lines = iter(physical_lines)
        self.status = status

    def __enter__(self) -> "FakeSSEBody":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def readline(self) -> bytes:
        return next(self.lines, b"")


def sse_response_for(value: dict[str, object]) -> FakeSSEBody:
    content = json.dumps(value, ensure_ascii=False)
    cut1 = max(1, len(content) // 3)
    cut2 = max(cut1 + 1, len(content) * 2 // 3)
    parts = [content[:cut1], content[cut1:cut2], content[cut2:]]
    lines: list[bytes] = []
    lines.append(
        b'event: reasoning.delta\ndata: {"type":"reasoning.delta","content":"\xe4\xb8\x8d\xe5\xba\x94\xe8\xbd\xac\xe5\x8f\x91\xe7\x9a\x84\xe6\x80\x9d\xe8\x80\x83"}\n\n'
    )
    for part in parts:
        chunk = {"type": "message.delta", "content": part}
        lines.append(
            (
                "event: message.delta\ndata: "
                + json.dumps(chunk, ensure_ascii=False)
                + "\n\n"
            ).encode("utf-8")
        )
    end = {
        "type": "chat.end",
        "result": {
            "model_instance_id": "h3-script-editor",
            "output": [{"type": "message", "content": content}],
            "stats": {
                "input_tokens": 120,
                "total_output_tokens": 80,
                "reasoning_output_tokens": 16,
                "tokens_per_second": 12.5,
            },
        },
    }
    lines.append(
        (
            "event: chat.end\ndata: "
            + json.dumps(end, ensure_ascii=False)
            + "\n\n"
        ).encode("utf-8")
    )
    return FakeSSEBody(lines)


class ConfigTests(unittest.TestCase):
    def test_local_provider_needs_no_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "base_url": app.DEFAULT_BASE_URL,
                        "model": app.DEFAULT_MODEL,
                        "identifier": app.DEFAULT_IDENTIFIER,
                        "context_length": 131072,
                        "reasoning": "on",
                        "stream": True,
                    }
                ),
                encoding="utf-8",
            )
            settings = app.resolve_provider_settings(
                config_path=config, secrets_path=root / "missing.env"
            )
            self.assertEqual(settings.base_url, "http://127.0.0.1:1234/api/v1")
            self.assertEqual(settings.identifier, "h3-script-editor")

    def test_project_config_is_fixed_to_local_model(self) -> None:
        config = app.load_config()
        self.assertEqual(config["provider"], "lmstudio")
        self.assertEqual(config["model"], "qwen3.6-27b-uncensored-hauhaucs-aggressive")
        self.assertEqual(config["identifier"], "h3-script-editor")
        self.assertEqual(config["context_length"], 131072)
        self.assertTrue(config["stream"])
        self.assertEqual(config["reasoning"], "on")
        self.assertEqual(config["studio_port"], 8794)
        self.assertEqual(config["lmstudio_port"], 1234)
        self.assertEqual(config["comfyui_port"], 8188)
        self.assertTrue(config["lmstudio_auto_start"])
        self.assertNotIn("base_url", config)
        self.assertNotIn("max_tokens", config)
        self.assertNotIn("api_key", config)
        self.assertNotIn("comfyui_root", config)

    def test_custom_ports_drive_all_local_endpoints_and_preserve_other_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config = app.load_config()
            config_path.write_text(json.dumps(config), encoding="utf-8")
            saved = app.save_port_settings(
                {
                    "studio_port": 18794,
                    "lmstudio_port": 11234,
                    "comfyui_port": 18188,
                    "lmstudio_auto_start": False,
                },
                config_path=config_path,
            )
            self.assertEqual(saved.as_ports(), {"studio": 18794, "lmstudio": 11234, "comfyui": 18188})
            provider = app.resolve_provider_settings(config_path=config_path)
            self.assertEqual(provider.base_url, "http://127.0.0.1:11234/api/v1")
            self.assertFalse(provider.auto_start_server)
            persisted = app.load_config(config_path)
            self.assertEqual(persisted["model"], app.DEFAULT_MODEL)
            self.assertNotIn("base_url", persisted)

            server = app.create_server(
                "127.0.0.1",
                0,
                config_path=config_path,
                runs_root=Path(temp_dir) / "runs",
            )
            try:
                self.assertEqual(
                    server.long_runtime.comfy_client_factory().base_url,
                    "http://127.0.0.1:18188",
                )
                self.assertEqual(
                    server.context_runtime.comfy_client_factory().base_url,
                    "http://127.0.0.1:18188",
                )
            finally:
                server.server_close()

    def test_duplicate_or_invalid_ports_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(json.dumps(app.load_config()), encoding="utf-8")
            with self.assertRaises(app.StudioError) as duplicate:
                app.save_port_settings(
                    {"studio_port": 9000, "lmstudio_port": 9000, "comfyui_port": 8188},
                    config_path=config_path,
                )
            self.assertEqual(duplicate.exception.code, "port_conflict")
            with self.assertRaises(app.StudioError) as invalid:
                app.save_port_settings(
                    {"studio_port": 0, "lmstudio_port": 1234, "comfyui_port": 8188},
                    config_path=config_path,
                )
            self.assertEqual(invalid.exception.code, "port_invalid")


class ProviderMockTests(unittest.TestCase):
    def test_stream_request_parameters_and_usage(self) -> None:
        captured: dict[str, object] = {}

        def fake_open(request: urllib.request.Request, timeout: int) -> FakeSSEBody:
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["authorization"] = request.headers.get("Authorization")
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return sse_response_for({"mode": "T2VA", "prompt": "ok", "warnings": []})

        events: list[tuple[str, dict[str, object]]] = []
        settings = lmstudio_runtime.LMStudioSettings()
        manager = lmstudio_runtime.LMStudioSessionManager(
            settings=settings, cli_path=PROJECT_ROOT / "tests" / "fake-lms.exe"
        )
        with mock.patch.object(manager, "request_session", return_value=nullcontext()):
            result, usage, finish = manager.provider_call(
                [{"role": "user", "content": "return json"}],
                settings,
                event_callback=lambda name, data: events.append((name, data)),
                urlopen_func=fake_open,
            )
        self.assertEqual(result["mode"], "T2VA")
        self.assertEqual(finish, "stop")
        self.assertEqual(usage["input_tokens"], 120)
        self.assertEqual(usage["total_output_tokens"], 80)
        self.assertEqual(usage["reasoning_output_tokens"], 16)
        payload = captured["payload"]
        self.assertEqual(payload["model"], "h3-script-editor")
        self.assertEqual(captured["url"], "http://127.0.0.1:1234/api/v1/chat")
        self.assertTrue(payload["stream"])
        self.assertFalse(payload["store"])
        self.assertEqual(payload["reasoning"], "on")
        self.assertEqual(payload["input"][0]["type"], "text")
        self.assertIn("Return exactly one valid JSON object", payload["system_prompt"])
        self.assertNotIn("messages", payload)
        self.assertNotIn("response_format", payload)
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("enable_thinking", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("max_output_tokens", payload)
        self.assertIsNone(captured["authorization"])
        streamed = "".join(data["text"] for name, data in events if name == "delta")
        self.assertNotIn("不应转发的思考", streamed)
        thinking_events = [data for name, data in events if name == "thinking"]
        self.assertEqual(len(thinking_events), 1)
        self.assertIn("正在思考", thinking_events[0]["message"])
        self.assertEqual(thinking_events[0]["text"], "不应转发的思考")
        self.assertEqual(json.loads(streamed)["mode"], "T2VA")

    def test_native_provider_converts_picture_data_url(self) -> None:
        captured: dict[str, object] = {}

        def fake_open(request: urllib.request.Request, timeout: int) -> FakeSSEBody:
            self.assertGreater(timeout, 0)
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return sse_response_for({"pictures": [{"id": "Picture 1", "description": "x"}]})

        manager = lmstudio_runtime.LMStudioSessionManager()
        messages = [
            {"role": "system", "content": "Return JSON."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe Picture 1."},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AA=="},
                    },
                ],
            },
        ]
        with mock.patch.object(manager, "request_session", return_value=nullcontext()):
            manager.provider_call(messages, urlopen_func=fake_open)
        native_input = captured["payload"]["input"]
        self.assertEqual(native_input[0], {"type": "text", "content": "Describe Picture 1."})
        self.assertEqual(native_input[1], {"type": "image", "data_url": "data:image/png;base64,AA=="})

    def test_native_provider_serializes_assistant_history_for_repair(self) -> None:
        system_prompt, native_input = lmstudio_runtime._native_chat_parts(
            [
                {"role": "system", "content": "Return repaired JSON."},
                {"role": "user", "content": '{"task":"first_attempt"}'},
                {"role": "assistant", "content": '{"broken":true}'},
                {"role": "user", "content": '{"task":"repair"}'},
            ]
        )
        self.assertIn("Return repaired JSON", system_prompt)
        self.assertIn("Execute ONLY current_request", system_prompt)
        self.assertEqual(len(native_input), 1)
        envelope = json.loads(native_input[0]["content"])
        self.assertEqual(envelope["envelope_type"], "repair_conversation")
        self.assertEqual(
            envelope["history"],
            [
                {"role": "user", "content": '{"task":"first_attempt"}'},
                {"role": "assistant", "content": '{"broken":true}'},
            ],
        )
        self.assertEqual(envelope["current_request"], '{"task":"repair"}')

    def test_mock_qwen_responses_for_all_h3_modes(self) -> None:
        settings = lmstudio_runtime.LMStudioSettings()
        manager = lmstudio_runtime.LMStudioSessionManager(settings=settings)
        for mode in ("T2VA", "I2VA", "FL2VA"):
            expected = {"mode": mode, "prompt": valid_prompt(mode), "warnings": []}
            with mock.patch.object(manager, "request_session", return_value=nullcontext()):
                parsed, _, _ = manager.provider_call(
                    [{"role": "user", "content": "json"}],
                    settings,
                    urlopen_func=lambda *_args, value=expected, **_kwargs: sse_response_for(value),
                )
            normalized = app.normalize_compile_result(
                parsed,
                app.sanitize_form(base_form(mode)),
                app.normalize_script_result(script_result(), app.sanitize_form(base_form(mode)))["script"],
            )
            self.assertTrue(normalized["validation"]["valid"], normalized["validation"]["errors"])

    def test_empty_content_is_not_retried(self) -> None:
        calls = 0

        def fake_open(*_args: object, **_kwargs: object) -> FakeSSEBody:
            nonlocal calls
            calls += 1
            return FakeSSEBody(
                [
                    b'event: chat.end\ndata: {"type":"chat.end","result":{"output":[],"stats":{}}}\n\n',
                ]
            )

        manager = lmstudio_runtime.LMStudioSessionManager()
        with mock.patch.object(manager, "request_session", return_value=nullcontext()):
            with self.assertRaises(lmstudio_runtime.LMStudioError) as caught:
                manager.provider_call(
                    [{"role": "user", "content": "json"}],
                    urlopen_func=fake_open,
                )
        self.assertEqual(calls, 1)
        self.assertEqual(caught.exception.code, "provider_content_empty")


class LMStudioLifecycleTests(unittest.TestCase):
    def _manager_fixture(
        self,
        *,
        loaded: list[dict[str, object]] | None = None,
        settings: lmstudio_runtime.LMStudioSettings | None = None,
        port_in_use: bool = False,
    ):
        temporary = tempfile.TemporaryDirectory()
        cli = Path(temporary.name) / "lms.exe"
        cli.write_bytes(b"")
        state = {
            "loaded": list(loaded or []),
            "commands": [],
            "server": True,
        }

        def runner(**kwargs: object) -> SimpleNamespace:
            command = list(kwargs["args"])
            args = command[1:]
            state["commands"].append(args)
            if args[:2] == ["ls", "--json"]:
                output = [
                    {
                        "type": "llm",
                        "modelKey": lmstudio_runtime.DEFAULT_MODEL,
                        "path": (
                            "HauhauCS/Qwen3.6-27B-Uncensored-HauhauCS-Aggressive/"
                            "Qwen3.6-27B-Uncensored-HauhauCS-Aggressive-Q4_K_P.gguf"
                        ),
                    }
                ]
            elif args[:2] == ["ps", "--json"]:
                output = state["loaded"]
            elif args and args[0] == "load":
                identifier = args[args.index("--identifier") + 1]
                state["loaded"] = [
                    {
                        "identifier": identifier,
                        "modelKey": lmstudio_runtime.DEFAULT_MODEL,
                    }
                ]
                output = []
            elif args and args[0] == "unload":
                identifier = args[1]
                state["loaded"] = [
                    item for item in state["loaded"]
                    if item.get("identifier") != identifier
                ]
                output = []
            elif args[:2] == ["server", "start"]:
                state["server"] = True
                output = []
            else:
                raise AssertionError(args)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(output),
                stderr="",
            )

        def local_api(*_args: object, **_kwargs: object):
            if not state["server"]:
                raise OSError("server down")
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.status = 200
            response.read.return_value = b'{"models":[]}'
            return response

        manager = lmstudio_runtime.LMStudioSessionManager(
            settings=settings,
            cli_path=cli,
            command_runner=runner,
            urlopen_func=local_api,
            port_in_use_func=lambda _host, _port: port_in_use,
            sleep_func=lambda _seconds: None,
        )
        return temporary, manager, state

    def test_load_uses_fixed_owned_identifier_and_no_ttl_then_unloads_only_it(self) -> None:
        temporary, manager, state = self._manager_fixture()
        self.addCleanup(temporary.cleanup)
        status = manager.ensure_ready()
        self.assertTrue(status["model"]["owned_loaded"])
        load = next(args for args in state["commands"] if args[0] == "load")
        self.assertEqual(load[1], lmstudio_runtime.DEFAULT_MODEL)
        self.assertIn("--gpu", load)
        self.assertEqual(load[load.index("--gpu") + 1], "max")
        self.assertEqual(load[load.index("--context-length") + 1], "131072")
        self.assertEqual(load[load.index("--parallel") + 1], "1")
        self.assertEqual(
            load[load.index("--identifier") + 1],
            lmstudio_runtime.DEFAULT_IDENTIFIER,
        )
        self.assertNotIn("--ttl", load)
        released = manager.release()
        self.assertFalse(released["model"]["owned_loaded"])
        unloads = [args for args in state["commands"] if args[0] == "unload"]
        self.assertEqual(unloads, [["unload", lmstudio_runtime.DEFAULT_IDENTIFIER]])

    def test_server_start_uses_documented_cli_directly_without_daemon_command(self) -> None:
        temporary, manager, state = self._manager_fixture()
        self.addCleanup(temporary.cleanup)
        state["server"] = False
        manager.ensure_ready()
        starts = [args for args in state["commands"] if args[:2] == ["server", "start"]]
        self.assertEqual(
            starts,
            [["server", "start", "--port", "1234", "--bind", "127.0.0.1"]],
        )
        self.assertFalse(any(args and args[0] == "daemon" for args in state["commands"]))

    def test_server_only_start_uses_custom_port_without_loading_model(self) -> None:
        settings = lmstudio_runtime.LMStudioSettings(
            base_url="http://127.0.0.1:22345/api/v1"
        )
        temporary, manager, state = self._manager_fixture(settings=settings)
        self.addCleanup(temporary.cleanup)
        state["server"] = False
        status = manager.ensure_server()
        self.assertTrue(status["server"]["running"])
        self.assertIn(
            ["server", "start", "--port", "22345", "--bind", "127.0.0.1"],
            state["commands"],
        )
        self.assertFalse(any(args and args[0] == "load" for args in state["commands"]))

    def test_occupied_non_lmstudio_port_requires_user_choice(self) -> None:
        temporary, manager, state = self._manager_fixture(port_in_use=True)
        self.addCleanup(temporary.cleanup)
        state["server"] = False
        with self.assertRaises(lmstudio_runtime.LMStudioError) as caught:
            manager.ensure_server()
        self.assertEqual(caught.exception.code, "lmstudio_port_occupied")
        self.assertFalse(any(args[:2] == ["server", "start"] for args in state["commands"]))

    def test_auto_start_can_be_disabled(self) -> None:
        settings = lmstudio_runtime.LMStudioSettings(auto_start_server=False)
        temporary, manager, state = self._manager_fixture(settings=settings)
        self.addCleanup(temporary.cleanup)
        state["server"] = False
        with self.assertRaises(lmstudio_runtime.LMStudioError) as caught:
            manager.ensure_server()
        self.assertEqual(caught.exception.code, "lmstudio_server_not_running")
        self.assertFalse(any(args[:2] == ["server", "start"] for args in state["commands"]))

    def test_external_same_model_instance_is_blocked_and_never_unloaded(self) -> None:
        temporary, manager, state = self._manager_fixture(
            loaded=[{"identifier": "chatbox", "modelKey": lmstudio_runtime.DEFAULT_MODEL}]
        )
        self.addCleanup(temporary.cleanup)
        with self.assertRaises(lmstudio_runtime.LMStudioError) as caught:
            manager.ensure_ready()
        self.assertEqual(caught.exception.code, "lmstudio_model_conflict")
        self.assertFalse(any(args[0] in {"load", "unload"} for args in state["commands"]))

    def test_release_refuses_while_provider_request_is_active(self) -> None:
        temporary, manager, _state = self._manager_fixture()
        self.addCleanup(temporary.cleanup)
        with manager.request_session():
            with self.assertRaises(lmstudio_runtime.LMStudioError) as caught:
                manager.release()
        self.assertEqual(caught.exception.code, "lmstudio_requests_active")
        manager.release()

    def test_authoring_confirmation_fingerprint_expires_after_edit(self) -> None:
        project = make_story_project("fingerprint_test", 5)
        longform.confirm_authoring(
            project,
            provider="lmstudio",
            model=lmstudio_runtime.DEFAULT_MODEL,
        )
        self.assertTrue(longform.authoring_confirmation_is_current(project))
        project["segments"][0]["story_card"]["story_text"] += " 修改"
        self.assertFalse(longform.authoring_confirmation_is_current(project))


class ScriptAndPromptTests(unittest.TestCase):
    def test_creative_brief_alone_allows_optional_fields_to_be_empty(self) -> None:
        raw = base_form("T2VA")
        for name in (
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
        ):
            raw[name] = ""
        form = app.sanitize_form(raw)
        app.ensure_creative_input(form)
        payload = json.loads(app.build_script_messages(form)[1]["content"])
        self.assertEqual(payload["input"]["creative_brief"], raw["creative_brief"])
        self.assertEqual(payload["input"]["visual_style"], "")

    def test_script_preserves_dialogue_and_visible_text(self) -> None:
        form = app.sanitize_form(base_form())
        normalized = app.normalize_script_result(script_result(), form)
        shot = normalized["script"]["shots"][0]
        self.assertEqual(shot["dialogue"][0]["text"], "你好，世界！")
        self.assertEqual(shot["visible_text"][0], "营业中")

    def test_script_rejects_changed_exact_dialogue(self) -> None:
        value = script_result()
        value["script"]["shots"][0]["dialogue"][0]["text"] = "你好世界"
        with self.assertRaises(app.StudioError) as caught:
            app.normalize_script_result(value, app.sanitize_form(base_form()))
        self.assertEqual(caught.exception.code, "script_dialogue_not_preserved")

    def test_script_rejects_action_arrays_and_timeline_gaps(self) -> None:
        form = app.sanitize_form(base_form())
        array_value = script_result()
        array_value["script"]["shots"][0]["action"] = ["错误数组"]
        with self.assertRaises(app.StudioError) as array_error:
            app.normalize_script_result(array_value, form)
        self.assertEqual(array_error.exception.code, "script_schema_invalid")

        gap_value = script_result()
        first = deepcopy(gap_value["script"]["shots"][0])
        second = deepcopy(first)
        first.update({"shot": 1, "start": 0, "end": 2})
        second.update(
            {
                "shot": 2,
                "start": 2.5,
                "end": 7,
                "dialogue": [],
                "visible_text": [],
            }
        )
        gap_value["script"]["shots"] = [first, second]
        with self.assertRaises(app.StudioError) as gap_error:
            app.normalize_script_result(gap_value, form)
        self.assertEqual(gap_error.exception.code, "script_timeline_invalid")
        self.assertIn("空档", gap_error.exception.message)

    def test_three_mode_formats_and_field_order(self) -> None:
        dialogue = [("Chinese", "你好，世界！")]
        for mode in ("T2VA", "I2VA", "FL2VA"):
            result = app.validate_h3_prompt(
                mode, valid_prompt(mode), 7.0, 1, dialogue, ["营业中"]
            )
            self.assertTrue(result["valid"], result["errors"])

    def test_wrong_field_order_and_ref2va_are_rejected(self) -> None:
        prompt = valid_prompt("T2VA").replace(
            "integrated_multimodal_description:", "subject_definitions:\nX\n\noverall_soundscape:", 1
        )
        result = app.validate_h3_prompt("T2VA", prompt, 7.0, 1, [], [])
        self.assertFalse(result["valid"])
        self.assertTrue(any("Ref2VA" in item for item in result["errors"]))

    def test_timestamp_and_dialogue_changes_are_rejected(self) -> None:
        prompt = valid_prompt("T2VA").replace(
            "A speaker", "[Shot 2] At 00:08.000, a speaker"
        ).replace("你好，世界！", "你好世界")
        result = app.validate_h3_prompt(
            "T2VA", prompt, 7.0, 2, [("Chinese", "你好，世界！")], ["营业中"]
        )
        self.assertFalse(result["valid"])
        self.assertTrue(any("超过视频时长" in item for item in result["errors"]))
        self.assertTrue(any("完整保留" in item for item in result["errors"]))

    def test_fl2va_uses_two_decimal_end_time(self) -> None:
        prompt = valid_prompt("FL2VA").replace("7.00-second", "7-second")
        result = app.validate_h3_prompt(
            "FL2VA", prompt, 7.0, 1, [("Chinese", "你好，世界！")], ["营业中"]
        )
        self.assertFalse(result["valid"])
        self.assertTrue(any("FL2VA 第一行" in item for item in result["errors"]))
        frame_exact_form = base_form("FL2VA")
        frame_exact_form["duration"] = 136 / 24
        frame_exact_form["exact_dialogue"] = ""
        frame_exact_form["visible_text"] = ""
        rounded_prompt = dynamic_valid_prompt(frame_exact_form)
        rounded_result = app.validate_h3_prompt(
            "FL2VA", rounded_prompt, 136 / 24, 1, [], []
        )
        self.assertTrue(rounded_result["valid"], rounded_result["errors"])

    def test_compiler_uses_local_skill_and_base_only(self) -> None:
        form = app.sanitize_form(base_form("I2VA"))
        script = app.normalize_script_result(script_result(), form)["script"]
        messages = app.build_compiler_messages(form, script)
        system = messages[0]["content"]
        self.assertIn("# H3 Prompt Writing", system)
        self.assertIn("# Video Prompt Writing Guide", system)
        self.assertNotIn("# Full-Reference Mode Rewrite Output Format Guide", system)
        self.assertNotIn("data:image", json.dumps(messages, ensure_ascii=False))

    def test_custom_prompts_prioritize_one_continuous_shot(self) -> None:
        scriptwriter = (PROJECT_ROOT / "prompts" / "scriptwriter.md").read_text(
            encoding="utf-8"
        )
        compiler = (PROJECT_ROOT / "prompts" / "compiler.md").read_text(
            encoding="utf-8"
        )
        long_segment = (PROJECT_ROOT / "prompts" / "long_segment.md").read_text(
            encoding="utf-8"
        )
        long_compiler = (PROJECT_ROOT / "prompts" / "long_compiler.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("默认一镜到底", scriptwriter)
        self.assertIn("不得自行新增 `[Shot 2]`", compiler)
        self.assertIn("Prefer one continuous take", long_segment)
        self.assertIn("Default to one continuous `[Shot 1]`", long_compiler)


class VisionTests(unittest.TestCase):
    @staticmethod
    def image(mime: str, payload: bytes) -> dict[str, str]:
        return {"data_url": f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"}

    def test_image_count_and_magic(self) -> None:
        png = self.image("image/png", b"\x89PNG\r\n\x1a\nmock")
        jpeg = self.image("image/jpeg", b"\xff\xd8\xffmock")
        self.assertEqual(len(app.validate_images([png], "I2VA")), 1)
        self.assertEqual(len(app.validate_images([png, jpeg], "FL2VA")), 2)
        with self.assertRaises(app.StudioError):
            app.validate_images([png], "FL2VA")
        with self.assertRaises(app.StudioError):
            app.validate_images([self.image("image/png", b"not-png")], "I2VA")

    def test_project_assets_are_content_addressed_and_path_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = longform.LongProjectStore(root / "runs")
            project = make_story_project(project_id="asset_test", seconds=5)
            store.save(project)
            assets = project_assets.ProjectAssetStore(store)
            payload = b"\x89PNG\r\n\x1a\nmock"
            saved = assets.save_data_url(
                "asset_test",
                original_name="首帧.png",
                data_url="data:image/png;base64,"
                + base64.b64encode(payload).decode("ascii"),
            )
            self.assertEqual(saved["source"], "project_asset")
            self.assertEqual(saved["asset_id"], hashlib.sha256(payload).hexdigest())
            resolved = assets.resolve("asset_test", saved)
            self.assertEqual(resolved.read_bytes(), payload)
            self.assertEqual(
                resolved.parent, store.project_dir("asset_test") / "assets"
            )
            public = assets.public("asset_test", saved)
            self.assertTrue(public["url"].endswith(saved["asset_id"]))
            escaped = {**saved, "relative_path": "assets/../project.json"}
            with self.assertRaises(longform.LongFormError):
                assets.resolve("asset_test", escaped)
            self.assertFalse(hasattr(app, "list_comfyui_input_images"))

    def test_vision_result_schema(self) -> None:
        value = {
            "pictures": [
                {"id": "Picture 1", "description": "首图", "visible_text": []},
                {"id": "Picture 2", "description": "尾图", "visible_text": ["原文"]},
            ],
            "transition_observations": "姿态变化",
            "warnings": [],
        }
        normalized = app.normalize_vision_result(value, "FL2VA")
        self.assertEqual(normalized["pictures"][1]["id"], "Picture 2")
        self.assertEqual(normalized["transition_observations"], "姿态变化")


class LongFormPlanningTests(unittest.TestCase):
    def test_frame_balancing_is_exact_and_has_no_ten_segment_cap(self) -> None:
        plan = longform.allocate_segment_frames(58)
        self.assertEqual(plan.total_frames, 1392)
        self.assertEqual(plan.segment_count, 11)
        self.assertEqual(plan.segment_frames.count(127), 6)
        self.assertEqual(plan.segment_frames.count(126), 5)
        self.assertEqual(sum(plan.segment_frames), 1392)
        self.assertLessEqual(max(plan.segment_frames) - min(plan.segment_frames), 1)

        longer = longform.allocate_segment_frames(71)
        self.assertEqual(longer.segment_count, 14)
        self.assertEqual(sum(longer.segment_frames), 71 * 24)
        with self.assertRaises(longform.LongFormError):
            longform.allocate_segment_frames(60, segment_count=20)
        with self.assertRaises(longform.LongFormError):
            longform.allocate_segment_frames(60, segment_count=2)

    def test_short_final_duration_is_balanced_across_all_segments(self) -> None:
        plan = longform.allocate_segment_frames(20)
        self.assertEqual(plan.segment_frames, (120, 120, 120, 120))
        self.assertNotEqual(plan.segment_frames, (168, 168, 144))
        smoke = longform.allocate_segment_frames(17)
        self.assertEqual(smoke.segment_frames, (136, 136, 136))

    def test_seventeen_second_plan_creates_lightweight_story_cards(self) -> None:
        calls: list[int] = []

        def provider(messages, _settings, event_callback=None):
            if "long-form story architect" in messages[0]["content"]:
                return long_outline(17), {"total_tokens": 1}, "stop"
            context = json.loads(messages[1]["content"])
            index = int(context["segment_index"])
            calls.append(index)
            return long_story_card(
                index, story_target=context["story_target"]
            ), {"total_tokens": 1}, "stop"

        with tempfile.TemporaryDirectory() as temporary:
            store = longform.LongProjectStore(Path(temporary) / "runs")
            runtime = longform_runtime.LongFormRuntime(
                store=store,
                project_root=PROJECT_ROOT,
                provider_call=provider,
            )
            started = runtime.start_new_project(
                {"idea": "17 秒完整故事", "target_seconds": 17}, object()
            )
            task = wait_background_task(runtime, started["task"]["id"])
            self.assertEqual(task["state"], "completed", task)
            project = store.load(started["project_id"])
            self.assertEqual([item["frames"] for item in project["segments"]], [136, 136, 136])
            self.assertEqual(calls, [1, 2, 3])
            self.assertTrue(all(item["script_state"] == "planned" for item in project["segments"]))
            self.assertTrue(all(item["beats"] == [] for item in project["segments"]))
            self.assertEqual(project["segments"][0]["single_workspace"]["form"]["mode"], "T2VA")
            self.assertEqual(project["segments"][1]["single_workspace"]["form"]["mode"], "I2VA")
            self.assertEqual(
                project["segments"][1]["single_workspace"]["pictures"]["picture1"]["source"],
                "auto_tail",
            )

    def test_timeline_reorder_delete_add_preserves_total_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = longform.LongProjectStore(Path(temporary) / "runs")
            outline = longform.normalize_outline_result(long_outline(17), idea="时间线")
            project = longform.make_project(
                outline, longform.allocate_segment_frames(17), project_id="timeline_v3"
            )
            for index, placeholder in enumerate(project["segments"], start=1):
                card = longform.normalize_story_card_result(
                    long_story_card(index, story_target=placeholder["story_target"]),
                    index=index,
                    story_target=placeholder["story_target"],
                    previous_state="",
                )
                project["segments"][index - 1] = longform.apply_story_card(
                    placeholder, card
                )
            store.save(project)

            moved = longform.mutate_timeline(
                store,
                project,
                operation="move_to",
                segment_id="seg_0001",
                destination_index=3,
            )
            self.assertEqual(
                [item["story_card"]["title"] for item in moved["segments"]],
                ["剧情卡 2", "剧情卡 3", "剧情卡 1"],
            )
            self.assertEqual(sum(item["frames"] for item in moved["segments"]), 17 * 24)
            self.assertEqual(moved["segments"][0]["boundary_before"], "start")

            deleted = longform.mutate_timeline(
                store, moved, operation="delete", segment_id="seg_0002"
            )
            self.assertEqual([item["frames"] for item in deleted["segments"]], [136, 272])
            added = longform.mutate_timeline(
                store, deleted, operation="add_after", segment_id="seg_0001"
            )
            self.assertEqual([item["frames"] for item in added["segments"]], [136, 136, 136])
            with self.assertRaises(longform.LongFormError) as caught:
                longform.mutate_timeline(
                    store, added, operation="split", segment_id="seg_0001"
                )
            self.assertEqual(caught.exception.code, "segment_count_invalid")

    def test_single_workspace_materializes_only_after_script_and_prompt_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = longform.LongProjectStore(Path(temporary) / "runs")
            outline = longform.normalize_outline_result(long_outline(5), idea="单段绑定")
            project = longform.make_project(
                outline, longform.allocate_segment_frames(5), project_id="workspace_v3"
            )
            placeholder = project["segments"][0]
            card = longform.normalize_story_card_result(
                long_story_card(1, story_target=placeholder["story_target"]),
                index=1,
                story_target=placeholder["story_target"],
                previous_state="",
            )
            project["segments"][0] = longform.apply_story_card(placeholder, card)
            store.save(project)
            workspace = project["segments"][0]["single_workspace"]
            workspace["script"] = {
                "title": "单段",
                "logline": "完成动作",
                "duration": 5,
                "aspect_ratio": "9:16",
                "shots": [
                    {
                        "shot": 1,
                        "start": 0,
                        "end": 5,
                        "visual": "主体完成动作。",
                        "action": "主体从静止到完成。",
                        "camera": "稳定镜头。",
                        "dialogue": [],
                        "visible_text": [],
                        "sound": "自然环境声。",
                        "music": "N/A",
                    }
                ],
            }
            workspace["prompt"] = (
                "integrated_multimodal_description: [Shot 1] A subject completes one action.\n\n"
                "overall_soundscape: Natural ambience.\n\n"
                "non_diegetic_music: N/A"
            )
            workspace["validation"] = {"valid": True, "errors": [], "warnings": []}
            workspace["state"] = "valid"
            saved = longform.save_segment_workspace(
                store,
                project,
                "seg_0001",
                workspace,
                mark_script_change_dirty=False,
            )
            segment = saved["segments"][0]
            self.assertEqual(segment["script_state"], "ready")
            self.assertEqual(segment["timeline_state"], "valid")
            self.assertEqual(segment["prompt_state"], "valid")
            self.assertEqual(segment["beats"][-1]["end_frame"], 120)

    def test_four_outline_chapters_map_completely_to_three_segments(self) -> None:
        chapters = [
            {"chapter": index, "title": f"章 {index}", "summary": "推进", "turning_point": "转折"}
            for index in range(1, 5)
        ]
        targets = longform.build_segment_story_targets(
            chapters, ["闭合结尾", "保留最后一句对白"], 3
        )
        self.assertEqual(
            [item["chapter_numbers"] for item in targets],
            [[1], [2, 3], [4]],
        )
        self.assertFalse(targets[1]["must_close_story"])
        self.assertTrue(targets[2]["must_close_story"])
        self.assertEqual(
            targets[2]["required_ending_conditions"],
            ["闭合结尾", "保留最后一句对白"],
        )

    def test_seconds_beats_snap_to_frames_and_reject_invalid_timelines(self) -> None:
        beats = longform.normalize_beats(
            [
                {"start_seconds": 0, "end_seconds": 1.2, "action": "起步"},
                {"start_seconds": 1.2, "end_seconds": 6.67, "action": "完成"},
            ],
            frames=160,
        )
        self.assertEqual(
            [(item["start_frame"], item["end_frame"]) for item in beats],
            [(0, 29), (29, 160)],
        )
        self.assertEqual(beats[0]["end_seconds"], "1.208")
        self.assertEqual(beats[-1]["end_seconds"], "6.667")

        invalid_cases = [
            (
                [{"start_seconds": 0, "end_seconds": 10, "action": "越界"}],
                "segment_timeline_out_of_bounds",
            ),
            (
                [
                    {"start_seconds": 0, "end_seconds": 2, "action": "一"},
                    {"start_seconds": 2.5, "end_seconds": 6.67, "action": "二"},
                ],
                "segment_timeline_gap",
            ),
            (
                [{"start_seconds": 0, "end_seconds": 6.67, "action": ["数组"]}],
                "segment_beats_invalid",
            ),
            (
                [{"start_seconds": 0, "end_seconds": 6.6, "action": "过早结束"}],
                "segment_timeline_end_mismatch",
            ),
        ]
        for value, code in invalid_cases:
            with self.subTest(code=code), self.assertRaises(longform.LongFormError) as caught:
                longform.normalize_beats(value, frames=160)
            self.assertEqual(caught.exception.code, code)

    def test_v1_timed_action_migrates_to_needs_review_and_blocks_render(self) -> None:
        outline = longform.normalize_outline_result(long_outline(7), idea="迁移")
        project = longform.make_project(
            outline, longform.allocate_segment_frames(7), project_id="legacy_timeline"
        )
        story_target = project["segments"][0]["story_target"]
        project["segments"][0] = longform.normalize_segment_result(
            long_segment(1, story_target=story_target),
            index=1,
            frames=168,
            previous_state="",
            story_target=story_target,
        )
        project["status"] = "ready"
        project["schema_version"] = 1
        segment = project["segments"][0]
        segment["action"] = "0-10秒：完成一个实际只有七秒的动作。"
        for name in ("beats", "timeline_state", "legacy_action", "script_attempts"):
            segment.pop(name, None)
        migrated, changed = longform.migrate_project(project)
        self.assertTrue(changed)
        self.assertEqual(migrated["schema_version"], longform.SCHEMA_VERSION)
        self.assertEqual(migrated["segments"][0]["timeline_state"], "needs_review")
        longform.validate_project(migrated)

        with tempfile.TemporaryDirectory() as temporary:
            store = longform.LongProjectStore(Path(temporary) / "runs")
            store.save(migrated)
            runtime = longform_runtime.LongFormRuntime(
                store=store,
                project_root=PROJECT_ROOT,
                provider_call=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("provider must not run")
                ),
            )
            with self.assertRaises(longform.LongFormError) as caught:
                runtime.start_render("legacy_timeline", object())
            self.assertEqual(caught.exception.code, "project_timeline_not_ready")

    def test_early_v2_without_story_confirmation_requires_review(self) -> None:
        outline = longform.normalize_outline_result(long_outline(7), idea="早期 v2 迁移")
        project = longform.make_project(
            outline, longform.allocate_segment_frames(7), project_id="early_v2"
        )
        story_target = project["segments"][0]["story_target"]
        project["segments"][0] = longform.normalize_segment_result(
            long_segment(1, story_target=story_target),
            index=1,
            frames=168,
            previous_state="",
            story_target=story_target,
        )
        project["status"] = "ready"
        segment = project["segments"][0]
        for name in (
            "story_target",
            "covered_outline_chapters",
            "fulfilled_ending_requirements",
        ):
            segment.pop(name, None)

        migrated, changed = longform.migrate_project(project)
        self.assertTrue(changed)
        self.assertEqual(migrated["segments"][0]["timeline_state"], "needs_review")
        longform.validate_project(migrated)

        with tempfile.TemporaryDirectory() as temporary:
            store = longform.LongProjectStore(Path(temporary) / "runs")
            store.save(migrated)
            saved = longform.edit_segment(
                store,
                migrated,
                "seg_0001",
                {"beats": migrated["segments"][0]["beats"]},
            )
        reviewed = saved["segments"][0]
        self.assertEqual(reviewed["timeline_state"], "valid")
        self.assertEqual(
            reviewed["covered_outline_chapters"],
            reviewed["story_target"]["chapter_numbers"],
        )
        self.assertEqual(
            reviewed["fulfilled_ending_requirements"],
            reviewed["story_target"]["required_ending_conditions"],
        )

    def test_invalid_segment_is_repaired_once_and_audited(self) -> None:
        segment_calls = 0
        repair_payloads: list[dict[str, object]] = []

        def provider(messages, _settings, event_callback=None):
            nonlocal segment_calls
            if "long-form story architect" in messages[0]["content"]:
                return long_outline(7), {"total_tokens": 1}, "stop"
            context = json.loads(messages[1]["content"])
            segment_calls += 1
            if len(messages) > 2:
                repair_payloads.append(json.loads(messages[-1]["content"]))
            value = long_segment(
                1,
                frames=int(context["frames"]),
                story_target=context["story_target"],
            )
            if segment_calls == 1:
                value["segment"]["continuity_out"] = ""
            return value, {"total_tokens": 2}, "stop"

        with tempfile.TemporaryDirectory() as temporary:
            store = longform.LongProjectStore(Path(temporary) / "runs")
            runtime = longform_runtime.LongFormRuntime(
                store=store,
                project_root=PROJECT_ROOT,
                provider_call=provider,
            )
            started = runtime.start_new_project(
                {"idea": "自动修复", "target_seconds": 7}, object()
            )
            task = wait_background_task(runtime, started["task"]["id"])
            self.assertEqual(task["state"], "completed", task)
            project = store.load(started["project_id"])
            attempts = project["segments"][0]["script_attempts"]
            self.assertEqual([item["state"] for item in attempts], ["invalid", "accepted"])
            self.assertEqual(project["usage"]["total_tokens"], 5)
            self.assertEqual(repair_payloads[0]["task"], "repair_story_card_json")
            self.assertEqual(
                repair_payloads[0]["validation_error"]["code"],
                "story_card_invalid",
            )

    def test_three_invalid_segment_attempts_fail_without_completed_state(self) -> None:
        segment_calls = 0

        def provider(messages, _settings, event_callback=None):
            nonlocal segment_calls
            if "long-form story architect" in messages[0]["content"]:
                return long_outline(7), {"total_tokens": 1}, "stop"
            context = json.loads(messages[1]["content"])
            segment_calls += 1
            value = long_segment(
                1,
                frames=int(context["frames"]),
                story_target=context["story_target"],
            )
            value["segment"]["continuity_out"] = ""
            return value, {"total_tokens": 2}, "stop"

        with tempfile.TemporaryDirectory() as temporary:
            store = longform.LongProjectStore(Path(temporary) / "runs")
            runtime = longform_runtime.LongFormRuntime(
                store=store,
                project_root=PROJECT_ROOT,
                provider_call=provider,
            )
            started = runtime.start_new_project(
                {"idea": "持续错误", "target_seconds": 7}, object()
            )
            task = wait_background_task(runtime, started["task"]["id"])
            self.assertEqual(task["state"], "failed", task)
            self.assertEqual(task["error"]["code"], "segment_semantic_repair_exhausted")
            project = store.load(started["project_id"])
            self.assertEqual(project["status"], "failed")
            self.assertEqual(project["segments"][0]["script_state"], "failed")
            self.assertEqual(len(project["segments"][0]["script_attempts"]), 3)
            self.assertEqual(project["usage"]["total_tokens"], 7)
            self.assertEqual(segment_calls, 3)

    def test_final_segment_must_confirm_story_target_and_ending_requirements(self) -> None:
        segment_calls = 0
        repair_codes: list[str] = []

        def provider(messages, _settings, event_callback=None):
            nonlocal segment_calls
            if "long-form story architect" in messages[0]["content"]:
                return long_outline(7), {}, "stop"
            context = json.loads(messages[1]["content"])
            segment_calls += 1
            if len(messages) > 2:
                repair_codes.append(
                    json.loads(messages[-1]["content"])["validation_error"]["code"]
                )
            value = long_segment(
                1,
                frames=int(context["frames"]),
                story_target=context["story_target"],
            )
            if segment_calls == 1:
                value["segment"]["fulfilled_ending_requirements"] = []
            return value, {}, "stop"

        with tempfile.TemporaryDirectory() as temporary:
            store = longform.LongProjectStore(Path(temporary) / "runs")
            runtime = longform_runtime.LongFormRuntime(
                store=store,
                project_root=PROJECT_ROOT,
                provider_call=provider,
            )
            started = runtime.start_new_project(
                {"idea": "结尾目标", "target_seconds": 7}, object()
            )
            task = wait_background_task(runtime, started["task"]["id"])
            self.assertEqual(task["state"], "completed", task)
            self.assertEqual(repair_codes, ["segment_ending_requirements_mismatch"])
            project = store.load(started["project_id"])
            self.assertEqual(len(project["segments"][0]["script_attempts"]), 2)

    def test_long_exact_dialogue_must_exist_verbatim_before_render(self) -> None:
        outline = longform.normalize_outline_result(long_outline(7), idea="对白测试")
        project = longform.make_project(outline, longform.allocate_segment_frames(7), project_id="dialogue_test")
        story_target = project["segments"][0]["story_target"]
        project["segments"][0] = longform.normalize_segment_result(
            long_segment(1, frames=project["segments"][0]["frames"], story_target=story_target), index=1, frames=project["segments"][0]["frames"], previous_state="", story_target=story_target
        )
        project["exact_dialogue_required"] = ["原样保留这一句。"]
        with self.assertRaises(longform.LongFormError) as caught:
            longform.ensure_required_dialogue(project)
        self.assertEqual(caught.exception.code, "long_dialogue_not_preserved")
        project["segments"][0]["story_card"]["dialogue"] = [
            {"speaker": "甲", "language": "Chinese", "text": "原样保留这一句。"}
        ]
        longform.ensure_required_dialogue(project)
        project["segments"][0]["story_card"]["dialogue"].append(
            {"speaker": "甲", "language": "Chinese", "text": "原样保留这一句。"}
        )
        with self.assertRaises(longform.LongFormError) as duplicated:
            longform.ensure_required_dialogue(project)
        self.assertEqual(duplicated.exception.code, "long_dialogue_duplicated")

    def test_h3_mapping_keeps_endpoint_and_skips_duplicate_handoff(self) -> None:
        model_frames = longform.h3_model_frames(155, continuous=True)
        indices = longform.uniform_frame_indices(model_frames, 155, skip_first=True)
        self.assertEqual(model_frames % 17, 5)
        self.assertEqual(len(indices), 155)
        self.assertEqual(indices[0], 1)
        self.assertEqual(indices[-1], model_frames - 1)
        self.assertEqual(len(indices), len(set(indices)))

    def test_edit_marks_current_render_and_downstream_scripts_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = longform.LongProjectStore(Path(temporary))
            outline = longform.normalize_outline_result(long_outline(20), idea="测试")
            project = longform.make_project(outline, longform.allocate_segment_frames(20), project_id="edit_test")
            for index, placeholder in enumerate(project["segments"], start=1):
                project["segments"][index - 1] = longform.normalize_segment_result(
                    long_segment(index, frames=placeholder["frames"], story_target=placeholder["story_target"]), index=index, frames=placeholder["frames"], previous_state="前态", story_target=placeholder["story_target"]
                )
                project["segments"][index - 1]["render_state"] = "accepted"
            project["status"] = "completed"
            project["master"] = {"path": "old.mp4"}
            store.save(project)
            changed = longform.edit_segment(
                store,
                project,
                "seg_0002",
                {
                    "beats": [
                        {
                            "start_seconds": 0,
                            "end_seconds": project["segments"][1]["duration_seconds"],
                            "action": "手动新动作",
                        }
                    ]
                },
            )
            self.assertEqual(changed["segments"][1]["provenance"], "manual")
            self.assertEqual(changed["segments"][1]["render_state"], "stale")
            self.assertEqual(changed["segments"][2]["script_state"], "possibly_stale")
            self.assertEqual(changed["stale_from"], 2)
            self.assertEqual(changed["master"], {})
            self.assertTrue(changed["revision_history"])

    def test_outline_plus_one_call_per_segment_has_no_ten_segment_cap(self) -> None:
        calls: list[str] = []

        def provider(messages, _settings, event_callback=None):
            system = messages[0]["content"]
            if "long-form story architect" in system:
                calls.append("outline")
                return long_outline(71), {"total_tokens": 10}, "stop"
            context = json.loads(messages[1]["content"])
            index = int(context["segment_index"])
            calls.append(f"segment-{index}")
            if event_callback:
                event_callback("delta", {"text": f"segment {index}"})
            return long_segment(
                index,
                frames=int(context["frames"]),
                story_target=context["story_target"],
            ), {"total_tokens": 2}, "stop"

        with tempfile.TemporaryDirectory() as temporary:
            store = longform.LongProjectStore(Path(temporary) / "runs")
            runtime = longform_runtime.LongFormRuntime(
                store=store,
                project_root=PROJECT_ROOT,
                provider_call=provider,
            )
            result = runtime.start_new_project({"idea": "长段数测试", "target_seconds": 71}, object())
            task = wait_background_task(runtime, result["task"]["id"])
            self.assertEqual(task["state"], "completed", task)
            project = store.load(result["project_id"])
            self.assertEqual(len(project["segments"]), 14)
            self.assertEqual(calls[0], "outline")
            self.assertEqual(len(calls), 15)
            self.assertEqual(project["usage"]["total_tokens"], 38)

    def test_long_project_rejects_legacy_comfy_input_image_paths(self) -> None:
        calls: list[object] = []

        def provider(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("legacy image paths must fail before a provider call")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = longform.LongProjectStore(root / "runs")
            runtime = longform_runtime.LongFormRuntime(
                store=store,
                project_root=PROJECT_ROOT,
                provider_call=provider,
            )
            result = runtime.start_new_project(
                {
                    "idea": "有参考图的项目",
                    "target_seconds": 7,
                    "initial_frame": "opening.png",
                    "identity_references": ["person.png"],
                },
                object(),
            )
            task = wait_background_task(runtime, result["task"]["id"])
            self.assertEqual(task["state"], "failed", task)
            self.assertEqual(task["error"]["code"], "long_reference_api_removed")
            self.assertEqual(calls, [])

    def test_replan_preserves_selected_anchor_after_reindex(self) -> None:
        def provider(messages, _settings, event_callback=None):
            context = json.loads(messages[1]["content"])
            return long_segment(
                int(context["segment_index"]),
                frames=int(context["frames"]),
                story_target=context["story_target"],
            ), {}, "stop"

        with tempfile.TemporaryDirectory() as temporary:
            store = longform.LongProjectStore(Path(temporary) / "runs")
            outline = longform.normalize_outline_result(long_outline(42), idea="锚点测试")
            project = longform.make_project(outline, longform.allocate_segment_frames(42), project_id="anchor_test")
            for index, placeholder in enumerate(project["segments"], start=1):
                project["segments"][index - 1] = longform.normalize_segment_result(
                    long_segment(index, frames=placeholder["frames"], story_target=placeholder["story_target"]), index=index, frames=placeholder["frames"], previous_state="前态", story_target=placeholder["story_target"]
                )
            project["segments"][5]["summary"] = "必须保留的手动结局"
            project["segments"][5]["story_card"]["story_text"] = "必须保留的手动结局"
            project["segments"][5]["provenance"] = "manual"
            anchor_frames = project["segments"][5]["frames"]
            project["status"] = "stale"
            store.save(project)
            runtime = longform_runtime.LongFormRuntime(
                store=store,
                project_root=PROJECT_ROOT,
                provider_call=provider,
            )
            result = runtime.start_regeneration(
                "anchor_test",
                {
                    "edited_index": 2,
                    "keep_segment_ids": ["seg_0006"],
                    "duration_policy": "replan",
                    "new_segment_count": 5,
                },
                object(),
            )
            task = wait_background_task(runtime, result["task"]["id"])
            self.assertEqual(task["state"], "completed", task)
            updated = store.load("anchor_test")
            self.assertEqual(len(updated["segments"]), 5)
            anchors = [item for item in updated["segments"] if item["summary"] == "必须保留的手动结局"]
            self.assertEqual(len(anchors), 1)
            self.assertTrue(anchors[0]["locked"])
            self.assertEqual(anchors[0]["frames"], anchor_frames)
            self.assertEqual(sum(item["frames"] for item in updated["segments"]), updated["target_frames"])

    def test_fixed_regeneration_can_keep_every_later_segment_as_authoritative(self) -> None:
        def provider(*_args, **_kwargs):
            raise AssertionError("kept anchors must not call the provider")

        with tempfile.TemporaryDirectory() as temporary:
            store = longform.LongProjectStore(Path(temporary) / "runs")
            outline = longform.normalize_outline_result(long_outline(20), idea="全部保留")
            project = longform.make_project(outline, longform.allocate_segment_frames(20), project_id="keep_test")
            for index, placeholder in enumerate(project["segments"], start=1):
                project["segments"][index - 1] = longform.normalize_segment_result(
                    long_segment(index, frames=placeholder["frames"], story_target=placeholder["story_target"]), index=index, frames=placeholder["frames"], previous_state="前态", story_target=placeholder["story_target"]
                )
            project["status"] = "ready"
            store.save(project)
            project = longform.edit_segment(
                store,
                project,
                "seg_0001",
                {
                    "beats": [
                        {
                            "start_seconds": 0,
                            "end_seconds": project["segments"][0]["duration_seconds"],
                            "action": "手动确认第一段",
                        }
                    ]
                },
            )
            self.assertEqual(project["segments"][1]["script_state"], "possibly_stale")
            keep_ids = [item["id"] for item in project["segments"][1:]]
            runtime = longform_runtime.LongFormRuntime(
                store=store,
                project_root=PROJECT_ROOT,
                provider_call=provider,
            )
            result = runtime.start_regeneration(
                "keep_test",
                {
                    "edited_index": 1,
                    "keep_segment_ids": keep_ids,
                    "duration_policy": "fixed",
                },
                object(),
            )
            task = wait_background_task(runtime, result["task"]["id"])
            self.assertEqual(task["state"], "completed", task)
            updated = store.load("keep_test")
            self.assertEqual(updated["segments"][0]["script_state"], "ready")
            self.assertTrue(
                all(item["script_state"] == "planned" for item in updated["segments"][1:])
            )
            self.assertTrue(all(item["locked"] for item in updated["segments"][1:]))


class AtomicPersistenceTests(unittest.TestCase):
    def test_precompiler_repairs_script_and_prompt_once(self) -> None:
        project = make_story_project("repair_precompile", 5)
        segment = project["segments"][0]
        form = app.sanitize_form(segment["single_workspace"]["form"])
        calls: list[str] = []

        def provider_request(messages):
            payload = json.loads(messages[-1]["content"])
            task = str(payload.get("task") or "")
            calls.append(task)
            if task == "repair_single_segment_script_json":
                return dynamic_script_result(form), {"total_tokens": 1}
            if task == "repair_minimax_h3_prompt_json":
                return {
                    "mode": form["mode"],
                    "prompt": dynamic_valid_prompt(form),
                    "warnings": [],
                }, {"total_tokens": 1}
            if isinstance(payload.get("input"), dict):
                invalid = dynamic_script_result(form)
                invalid["script"]["shots"][0]["action"] = ["数组动作"]
                return invalid, {"total_tokens": 1}
            return {
                "mode": form["mode"],
                "prompt": (
                    "overall_soundscape: wrong order\n\n"
                    "integrated_multimodal_description: [Shot 1] invalid\n\n"
                    "non_diegetic_music: N/A"
                ),
                "warnings": [],
            }, {"total_tokens": 1}

        workspace, usage = app.precompile_segment_workspace(
            project, segment, provider_request
        )
        self.assertEqual(len(calls), 4)
        self.assertIn("repair_single_segment_script_json", calls)
        self.assertIn("repair_minimax_h3_prompt_json", calls)
        self.assertEqual(usage["total_tokens"], 4)
        self.assertEqual(workspace["usage"]["script"]["total_tokens"], 2)
        self.assertEqual(workspace["usage"]["compile"]["total_tokens"], 2)
        self.assertTrue(workspace["validation"]["valid"])

    def test_windows_read_polling_cannot_race_atomic_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = longform.LongProjectStore(Path(temporary) / "runs")
            store.save(make_story_project("read_write_lock", 5))
            stop = threading.Event()
            failures: list[Exception] = []

            def reader() -> None:
                while not stop.is_set():
                    try:
                        store.load("read_write_lock")
                        store.list_projects()
                    except Exception as exc:  # pragma: no cover - asserted below
                        failures.append(exc)
                        stop.set()

            readers = [threading.Thread(target=reader) for _ in range(4)]
            for thread in readers:
                thread.start()
            try:
                for index in range(40):
                    project = store.load("read_write_lock")
                    project["warnings"] = [f"write-{index}"]
                    store.save(project)
            finally:
                stop.set()
                for thread in readers:
                    thread.join(timeout=5)
            self.assertEqual(failures, [])
            self.assertEqual(store.load("read_write_lock")["warnings"], ["write-39"])

    def test_workspace_revision_is_atomic_compare_and_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = longform.LongProjectStore(Path(temporary) / "runs")
            store.save(make_story_project("workspace_cas", 5))
            barrier = threading.Barrier(3)
            outcomes: list[str] = []

            def writer(label: str) -> None:
                project = store.load("workspace_cas")
                workspace = deepcopy(project["segments"][0]["single_workspace"])
                workspace["form"]["extra_constraints"] = label
                barrier.wait()
                try:
                    longform.save_segment_workspace(
                        store,
                        project,
                        "seg_0001",
                        workspace,
                        expected_revision=0,
                    )
                    outcomes.append("saved:" + label)
                except longform.LongFormError as exc:
                    outcomes.append(exc.code)

            threads = [threading.Thread(target=writer, args=(label,)) for label in ("A", "B")]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=5)
            self.assertEqual(len([item for item in outcomes if item.startswith("saved:")]), 1)
            self.assertEqual(outcomes.count("single_workspace_conflict"), 1)
            saved = store.load("workspace_cas")["segments"][0]["single_workspace"]
            self.assertEqual(saved["revision"], 1)
            self.assertIn(saved["form"]["extra_constraints"], {"A", "B"})

    def test_bound_sse_emits_saved_receipt_before_result_and_survives_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs_root = Path(temporary) / "runs"
            store = longform.LongProjectStore(runs_root)
            store.save(make_story_project("sse_atomic", 5))
            server = app.create_server(
                "127.0.0.1",
                0,
                runs_root=runs_root,
                provider_call=fake_single_stage_provider,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address

            def post_sse(path: str, payload: dict[str, object]):
                request = urllib.request.Request(
                    f"http://{host}:{port}{path}",
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    return parse_local_sse(response.read().decode("utf-8"))

            try:
                project = store.load("sse_atomic")
                segment = project["segments"][0]
                form = segment["single_workspace"]["form"]
                script_events = post_sse(
                    "/api/script/stream",
                    {
                        "api_key": "offline-fake-key",
                        "form": form,
                        "binding": {
                            "project_id": project["id"],
                            "segment_id": segment["id"],
                            "workspace_revision": 0,
                        },
                        "workspace_pictures": segment["single_workspace"]["pictures"],
                    },
                )
                script_names = [name for name, _data in script_events]
                self.assertLess(script_names.index("saved"), script_names.index("result"))
                saved_script = next(data for name, data in script_events if name == "saved")
                self.assertEqual(saved_script["receipt"]["workspace_revision"], 1)
                self.assertEqual(store.load("sse_atomic")["segments"][0]["script_state"], "ready")

                project = store.load("sse_atomic")
                segment = project["segments"][0]
                compile_events = post_sse(
                    "/api/compile/stream",
                    {
                        "api_key": "offline-fake-key",
                        "form": segment["single_workspace"]["form"],
                        "script": segment["single_workspace"]["script"],
                        "binding": {
                            "project_id": project["id"],
                            "segment_id": segment["id"],
                            "workspace_revision": 1,
                        },
                        "workspace_pictures": segment["single_workspace"]["pictures"],
                    },
                )
                compile_names = [name for name, _data in compile_events]
                self.assertLess(compile_names.index("saved"), compile_names.index("result"))
                saved_prompt = next(data for name, data in compile_events if name == "saved")
                self.assertEqual(saved_prompt["receipt"]["workspace_revision"], 2)
                self.assertFalse(saved_prompt["receipt"]["readiness"]["ready"])
                self.assertEqual(
                    saved_prompt["receipt"]["readiness"]["blockers"][0]["code"],
                    "authoring_confirmation_required",
                )
                reloaded = store.load("sse_atomic")
                self.assertEqual(reloaded["segments"][0]["prompt_state"], "valid")
                self.assertTrue(reloaded["segments"][0]["h3_prompt"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_bound_results_are_saved_before_completion_with_revision_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = longform.LongProjectStore(Path(temporary) / "runs")
            store.save(make_story_project("atomic_bound", 5))
            project = store.load("atomic_bound")
            segment = project["segments"][0]
            form = app.sanitize_form(segment["single_workspace"]["form"])
            script_result_value = app.normalize_script_result(
                dynamic_script_result(form), form
            )

            saved_script, script_receipt = app.save_bound_stream_result(
                store,
                binding={
                    "project_id": project["id"],
                    "segment_id": segment["id"],
                    "workspace_revision": 0,
                },
                form=form,
                operation="script",
                result=script_result_value,
                script=script_result_value["script"],
                usage={"total_tokens": 11},
            )
            self.assertEqual(script_receipt["workspace_revision"], 1)
            self.assertEqual(script_receipt["states"]["script"], "ready")
            self.assertEqual(script_receipt["states"]["timeline"], "valid")
            self.assertEqual(script_receipt["states"]["prompt"], "pending")
            self.assertFalse(script_receipt["readiness"]["ready"])

            with self.assertRaises(app.StudioError) as conflict:
                app.save_bound_stream_result(
                    store,
                    binding={
                        "project_id": project["id"],
                        "segment_id": segment["id"],
                        "workspace_revision": 0,
                    },
                    form=form,
                    operation="script",
                    result=script_result_value,
                    script=script_result_value["script"],
                )
            self.assertEqual(conflict.exception.code, "single_workspace_conflict")
            self.assertEqual(conflict.exception.status, 409)

            compiled = app.normalize_compile_result(
                {
                    "mode": form["mode"],
                    "prompt": dynamic_valid_prompt(form),
                    "warnings": [],
                },
                form,
                script_result_value["script"],
            )
            saved_prompt, prompt_receipt = app.save_bound_stream_result(
                store,
                binding={
                    "project_id": project["id"],
                    "segment_id": segment["id"],
                    "workspace_revision": 1,
                },
                form=form,
                operation="compile",
                result=compiled,
                script=script_result_value["script"],
                usage={"total_tokens": 13},
            )
            self.assertEqual(prompt_receipt["workspace_revision"], 2)
            self.assertTrue(prompt_receipt["prompt_sha256"])
            self.assertEqual(prompt_receipt["states"]["workspace"], "valid")
            self.assertFalse(prompt_receipt["readiness"]["ready"])
            self.assertEqual(
                prompt_receipt["readiness"]["blockers"][0]["code"],
                "authoring_confirmation_required",
            )
            reloaded = store.load("atomic_bound")
            self.assertEqual(
                reloaded["segments"][0]["h3_prompt"], compiled["prompt"]
            )
            self.assertEqual(
                reloaded["segments"][0]["single_workspace"]["revision"], 2
            )
            self.assertEqual(saved_script["id"], saved_prompt["id"])

    def test_story_noop_preserves_prompt_and_real_change_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = longform.LongProjectStore(Path(temporary) / "runs")
            project = store.save(make_story_project("story_guard", 5))
            segment = project["segments"][0]
            workspace = deepcopy(segment["single_workspace"])
            form = app.sanitize_form(workspace["form"])
            script = app.normalize_script_result(dynamic_script_result(form), form)[
                "script"
            ]
            workspace.update(
                {
                    "script": script,
                    "prompt": dynamic_valid_prompt(form),
                    "validation": {"valid": True, "errors": [], "warnings": []},
                    "state": "valid",
                }
            )
            project = longform.save_segment_workspace(
                store,
                project,
                segment["id"],
                workspace,
                expected_revision=0,
                mark_script_change_dirty=False,
            )
            original_prompt = project["segments"][0]["h3_prompt"]
            original_revision = project["segments"][0]["single_workspace"]["revision"]

            unchanged = longform.edit_story_card(
                store,
                project,
                segment["id"],
                deepcopy(project["segments"][0]["story_card"]),
            )
            self.assertEqual(unchanged["segments"][0]["h3_prompt"], original_prompt)
            self.assertEqual(
                unchanged["segments"][0]["single_workspace"]["revision"],
                original_revision,
            )

            changed_card = deepcopy(project["segments"][0]["story_card"])
            changed_card["story_text"] += " 这是一次真实修改。"
            with self.assertRaises(longform.LongFormError) as confirmation:
                longform.edit_story_card(store, project, segment["id"], changed_card)
            self.assertEqual(
                confirmation.exception.code,
                "story_card_invalidation_confirmation_required",
            )
            self.assertEqual(store.load("story_guard")["segments"][0]["h3_prompt"], original_prompt)

            changed = longform.edit_story_card(
                store,
                project,
                segment["id"],
                changed_card,
                confirm_invalidate=True,
            )
            self.assertEqual(changed["segments"][0]["h3_prompt"], "")
            self.assertEqual(changed["segments"][0]["prompt_state"], "stale")
            self.assertEqual(changed["segments"][0]["single_workspace"]["state"], "stale")

    def test_server_precompile_persists_all_three_segments_and_enables_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = longform.LongProjectStore(Path(temporary) / "runs")
            store.save(make_story_project("precompile_three", 17))
            runtime = longform_runtime.LongFormRuntime(
                store=store,
                project_root=PROJECT_ROOT,
                provider_call=fake_single_stage_provider,
                segment_precompiler=app.precompile_segment_workspace,
            )
            started = runtime.start_precompile("precompile_three", object())
            task = wait_background_task(runtime, started["task"]["id"])
            self.assertEqual(task["state"], "completed", task)
            self.assertEqual(task["current"], 3)
            project = store.load("precompile_three")
            readiness = longform_runtime.compute_render_readiness(
                project, require_authoring_confirmation=False
            )
            self.assertTrue(readiness["ready"], readiness)
            self.assertEqual(readiness["ready_segments"], 3)
            self.assertEqual(project["status"], "ready")
            self.assertEqual(project["usage"]["total_tokens"], 72)
            for segment in project["segments"]:
                self.assertEqual(segment["script_state"], "ready")
                self.assertEqual(segment["timeline_state"], "valid")
                self.assertEqual(segment["prompt_state"], "valid")
                self.assertEqual(segment["single_workspace"]["state"], "valid")
                self.assertEqual(segment["single_workspace"]["revision"], 1)
                self.assertTrue(segment["h3_prompt"])


class LongPromptAndWorkflowTests(unittest.TestCase):
    def test_base_and_ref_prompt_schemas_are_kept_separate(self) -> None:
        segment = longform.normalize_segment_result(
            long_segment(2, frames=155), index=2, frames=155, previous_state="状态 1"
        )
        base = (
            "For the target video, at 0.00 seconds into the target video, "
            "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
            "integrated_multimodal_description: [Shot 1] A generic subject moves.\n\n"
            "overall_soundscape: Natural room tone.\n\n"
            "non_diegetic_music: N/A"
        )
        self.assertEqual(
            longform_runtime.validate_compiled_prompt(
                base, segment=segment, has_first_frame=True, reference_count=0
            ),
            [],
        )
        ref = (
            "subject_definitions: <Subject 1> is the person in <Picture 1>. "
            "<Picture 2> is the first frame of [Shot 1].\n\n"
            "summary: Preserve identity while continuing from <Picture 2>.\n\n"
            "retention_analysis: Keep face and wardrobe.\n\n"
            "detailed_description: [Shot 1] Continue the action.\n\n"
            "overall_soundscape: Natural room tone.\n\n"
            "non_diegetic_music: N/A"
        )
        self.assertEqual(
            longform_runtime.validate_compiled_prompt(
                ref, segment=segment, has_first_frame=True, reference_count=1
            ),
            [],
        )
        mixed = ref + "\n\nintegrated_multimodal_description: wrong"
        self.assertTrue(
            longform_runtime.validate_compiled_prompt(
                mixed, segment=segment, has_first_frame=True, reference_count=1
            )
        )

    def test_long_api_workflow_is_dynamic_frame_exact_and_hybrid(self) -> None:
        value = build_long_workflow.build_api_workflow(
            prompt="subject_definitions: A\nsummary: B\nretention_analysis: C\ndetailed_description: D\noverall_soundscape: E\nnon_diegetic_music: N/A",
            output_frames=155,
            boundary_before="continuous",
            seed=123,
            run_id="offline",
            segment_index=2,
            attempt=1,
            first_frame={"type": "output", "name": "tail.png [output]"},
            last_frame={"type": "input", "name": "ending.png"},
            reference_images=[{"type": "input", "name": "identity.png"}],
        )
        self.assertEqual(build_long_workflow.validate_api_workflow(value), [])
        meta = value["meta"]
        graph = value["prompt"]
        self.assertEqual(meta["output_frames"], 155)
        self.assertEqual(len(meta["selected_indices"]), 155)
        self.assertEqual(meta["selected_indices"][0], 1)
        self.assertEqual(meta["selected_indices"][-1], meta["model_frames"] - 1)
        self.assertEqual(meta["tail_source_index"], meta["model_frames"] - 1)
        self.assertEqual(graph["6"]["inputs"]["length"], meta["model_frames"])
        self.assertIn("first_frame", graph["6"]["inputs"])
        self.assertIn("last_frame", graph["6"]["inputs"])
        last_loader = graph[graph["6"]["inputs"]["last_frame"][0]]
        self.assertEqual(last_loader["class_type"], "LoadImage")
        self.assertEqual(last_loader["inputs"]["image"], "ending.png")
        self.assertIn("ref_images.ref_image_0", graph["6"]["inputs"])
        self.assertAlmostEqual(graph["14"]["inputs"]["duration"], 155 / 24)
        self.assertAlmostEqual(graph["14"]["inputs"]["start_index"], 1 / 24)
        self.assertEqual(meta["models"]["diffusion"], "minimax_h3_fl2va_pruned_int8_convrot.safetensors")
        self.assertEqual(meta["models"]["lora"], "minimax_h3_turbo_v4_step600_ema.safetensors")
        self.assertEqual(meta["models"]["steps"], 8)
        self.assertEqual(meta["models"]["final_resolution"], [1080, 1920])
        project_outputs = [
            node
            for node in graph.values()
            if node["class_type"].startswith("H3Idea2VideoProject")
        ]
        self.assertEqual(len(project_outputs), 4)
        self.assertTrue(
            all(
                node["inputs"]["project_root"] == "$IDEA2VIDEO_PROJECT_ROOT"
                for node in project_outputs
            )
        )
        video_outputs = [
            node
            for node in project_outputs
            if node["class_type"] == "H3Idea2VideoProjectVideoSave"
        ]
        self.assertEqual(len(video_outputs), 2)
        self.assertTrue(all(node["inputs"]["crf"] == 18.0 for node in video_outputs))

        saved = json.loads(
            (PROJECT_ROOT / "comfyui_workflows" / "MiniMax_H3_LongForm_AutoChain_API.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(build_long_workflow.validate_api_workflow(saved), [])
        self.assertEqual(saved["meta"]["output_frames"], 168)
        self.assertEqual(saved["meta"]["selected_indices"][0], 1)


class ComfyClientPreflightTests(unittest.TestCase):
    @staticmethod
    def object_info_fixture() -> dict[str, object]:
        info: dict[str, object] = {
            name: {"input": {"required": {}}}
            for name in longform_runtime.ComfyClient.REQUIRED_NODES
        }
        grouped: dict[tuple[str, str], list[str]] = {}
        for node_name, input_name, filename in longform_runtime.ComfyClient.REQUIRED_MODELS:
            grouped.setdefault((node_name, input_name), []).append(filename)
        for (node_name, input_name), choices in grouped.items():
            info[node_name]["input"]["required"][input_name] = [choices]
        return info

    def test_preflight_checks_nodes_and_exact_model_files(self) -> None:
        client = longform_runtime.ComfyClient()
        info = self.object_info_fixture()
        self.assertTrue(client.preflight(info=info)["ok"])
        info["UNETLoader"]["input"]["required"]["unet_name"] = [["other.safetensors"]]
        with self.assertRaises(longform.LongFormError) as missing:
            client.preflight(info=info)
        self.assertEqual(missing.exception.code, "comfy_models_missing")
        self.assertIn("minimax_h3_fl2va_pruned_int8_convrot.safetensors", missing.exception.message)

    def test_preflight_accepts_comfy_030_combo_model_options(self) -> None:
        client = longform_runtime.ComfyClient()
        info = self.object_info_fixture()
        info["UpscaleModelLoader"]["input"]["required"]["model_name"] = [
            "COMBO",
            {
                "multiselect": False,
                "options": [build_long_workflow.UPSCALE_MODEL],
            },
        ]
        self.assertTrue(client.preflight(info=info)["ok"])

        info["UpscaleModelLoader"]["input"]["required"]["model_name"][1][
            "options"
        ] = ["other.pth"]
        with self.assertRaises(longform.LongFormError) as missing:
            client.preflight(info=info)
        self.assertEqual(missing.exception.code, "comfy_models_missing")
        self.assertIn(build_long_workflow.UPSCALE_MODEL, missing.exception.message)

    def test_comfy_http_error_keeps_status_and_server_message(self) -> None:
        def rejected(_request, timeout):
            del timeout
            raise urllib.error.HTTPError(
                "http://127.0.0.1:8188/prompt",
                400,
                "Bad Request",
                {},
                io.BytesIO(json.dumps({"error": {"message": "bad node input"}}).encode()),
            )

        client = longform_runtime.ComfyClient(urlopen_func=rejected)
        with self.assertRaises(longform.LongFormError) as caught:
            client._request("/prompt", {"prompt": {}})
        self.assertEqual(caught.exception.code, "comfy_http_400")
        self.assertIn("bad node input", caught.exception.message)

    def test_missing_pyav_is_reported_before_media_production(self) -> None:
        with mock.patch(
            "longform_runtime.importlib.import_module", side_effect=ImportError("no av")
        ):
            with self.assertRaises(longform.LongFormError) as caught:
                longform_runtime.ensure_media_backend()
        self.assertEqual(caught.exception.code, "media_dependency_missing")
        self.assertIn("pip install av", caught.exception.message)


class LongRenderSchedulerTests(unittest.TestCase):
    def test_offline_mock_runs_segments_sequentially_and_reuses_native_tail(self) -> None:
        qc_image_counts: list[int] = []

        class FakeComfy:
            def __init__(self, project_root: Path) -> None:
                self.project_root = project_root
                self.workflows: list[dict[str, object]] = []
                self.uploads: list[Path] = []

            def preflight(self):
                return {"ok": True}

            def wait_until_idle(self, *, stop):
                self.assert_not_stopped = not stop()

            def submit(self, workflow):
                self.workflows.append(workflow)
                return f"prompt-{len(self.workflows)}"

            def upload_image(self, path, *, subfolder, filename=None):
                source = Path(path)
                self.uploads.append(source)
                return {
                    "type": "input",
                    "name": f"{subfolder}/{filename or source.name}",
                    "filename": filename or source.name,
                    "subfolder": subfolder,
                }

            def wait_history(self, prompt_id, *, stop):
                number = int(prompt_id.rsplit("-", 1)[1])
                workflow = self.workflows[number - 1]
                nodes = workflow["meta"]["output_nodes"]
                outputs = {}
                for kind, node_id in nodes.items():
                    node = workflow["prompt"][str(node_id)]
                    relative = node["inputs"]["relative_path"]
                    requested = self.project_root / "runs" / relative
                    requested.parent.mkdir(parents=True, exist_ok=True)
                    if kind == "qc_samples":
                        paths = [
                            requested.with_name(f"{requested.stem}_{index:04d}.png")
                            for index in range(1, 4)
                        ]
                    else:
                        paths = [requested]
                    for path in paths:
                        payload = (
                            b"\x89PNG\r\n\x1a\noffline"
                            if path.suffix == ".png"
                            else b"offline"
                        )
                        path.write_bytes(payload)
                    outputs[str(node_id)] = {
                        "text": [artifact_receipt(self.project_root, paths, kind)]
                    }
                return {"outputs": outputs, "status": {"completed": True}}

        def provider(messages, _settings, event_callback=None):
            system = messages[0]["content"]
            if "automated acceptance inspector" in system:
                qc_image_counts.append(
                    sum(1 for item in messages[1]["content"] if item.get("type") == "image_url")
                )
                checks = {
                    name: {"ok": True, "detail": "offline"}
                    for name in (
                        "handoff", "identity", "wardrobe_props", "planned_action_scene", "final_state"
                    )
                }
                return {"accepted": True, "score": 1.0, "checks": checks, "prompt_correction": "", "warnings": []}, {}, "stop"
            context = json.loads(messages[1]["content"])
            if context["fixed_reference_images"]:
                prompt = (
                    "subject_definitions: <Subject 1> is fixed by <Picture 1>. "
                    "<Picture 2> is the first frame of [Shot 1].\n\nsummary: Continue.\n\n"
                    "retention_analysis: Preserve identity.\n\ndetailed_description: [Shot 1] Move.\n\n"
                    "overall_soundscape: Natural ambience.\n\nnon_diegetic_music: N/A"
                )
                mode = "Ref2VA"
            elif context["has_first_frame"]:
                prompt = (
                    "For the target video, at 0.00 seconds into the target video, "
                    "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
                    "integrated_multimodal_description: [Shot 1] Continue.\n\n"
                    "overall_soundscape: Natural ambience.\n\nnon_diegetic_music: N/A"
                )
                mode = "I2VA"
            else:
                prompt = (
                    "integrated_multimodal_description: [Shot 1] Begin.\n\n"
                    "overall_soundscape: Natural ambience.\n\nnon_diegetic_music: N/A"
                )
                mode = "T2VA"
            return {"mode": mode, "prompt": prompt, "warnings": []}, {}, "stop"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = longform.LongProjectStore(root / "runs")
            outline = longform.normalize_outline_result(long_outline(12), idea="连续测试")
            project = longform.make_project(outline, longform.allocate_segment_frames(12), project_id="render_test")
            for index, placeholder in enumerate(project["segments"], start=1):
                project["segments"][index - 1] = longform.normalize_segment_result(
                    long_segment(index, character=True, frames=placeholder["frames"], story_target=placeholder["story_target"]),
                    index=index,
                    frames=placeholder["frames"],
                    previous_state="前态",
                    story_target=placeholder["story_target"],
                )
                segment = project["segments"][index - 1]
                if index == 1:
                    prompt = (
                        "integrated_multimodal_description: [Shot 1] Begin the planned action.\n\n"
                        "overall_soundscape: Natural ambience.\n\n"
                        "non_diegetic_music: N/A"
                    )
                    segment["single_workspace"]["form"]["mode"] = "T2VA"
                else:
                    prompt = (
                        "For the target video, at 0.00 seconds into the target video, "
                        "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
                        "integrated_multimodal_description: [Shot 1] Continue the planned action.\n\n"
                        "overall_soundscape: Natural ambience.\n\n"
                        "non_diegetic_music: N/A"
                    )
                    segment["single_workspace"]["form"]["mode"] = "I2VA"
                    segment["single_workspace"]["pictures"]["picture1"]["source"] = "auto_tail"
                segment["single_workspace"]["prompt"] = prompt
                segment["single_workspace"]["validation"] = {
                    "valid": True,
                    "errors": [],
                    "warnings": [],
                }
                segment["single_workspace"]["state"] = "valid"
                segment["h3_prompt"] = prompt
                segment["prompt_state"] = "valid"
            project["status"] = "ready"
            longform.confirm_authoring(
                project,
                provider="lmstudio",
                model=lmstudio_runtime.DEFAULT_MODEL,
            )
            store.save(project)
            fake_comfy = FakeComfy(root)

            def fake_inspector(path, *, expected_frames, expected_size, require_audio):
                return {
                    "path": str(path), "frames": expected_frames, "width": expected_size[0],
                    "height": expected_size[1], "has_audio": require_audio,
                }

            def fake_assembler(paths, destination, *, expected_frames):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"offline-master")
                return {"path": str(destination), "frames": expected_frames, "has_audio": True}

            provider_calls: list[object] = []

            def gpu_provider(*args, **_kwargs):
                provider_calls.append(args)
                raise AssertionError("GPU 阶段不得调用 Qwen")

            runtime = longform_runtime.LongFormRuntime(
                store=store,
                project_root=PROJECT_ROOT,
                provider_call=gpu_provider,
                comfy_client_factory=lambda: fake_comfy,
                media_inspector=fake_inspector,
                master_assembler=fake_assembler,
            )
            result = runtime.start_render("render_test", object())
            task = wait_background_task(runtime, result["task"]["id"])
            self.assertEqual(task["state"], "completed", task)
            self.assertEqual(len(fake_comfy.workflows), 2)
            first, second = fake_comfy.workflows
            self.assertEqual(first["meta"]["boundary_before"], "start")
            self.assertEqual(second["meta"]["boundary_before"], "continuous")
            self.assertEqual(second["meta"]["selected_indices"][0], 1)
            self.assertEqual(second["meta"]["tail_source_index"], second["meta"]["model_frames"] - 1)
            second_hybrid = second["prompt"]["6"]["inputs"]
            first_loader = second["prompt"][second_hybrid["first_frame"][0]]
            self.assertEqual(first_loader["class_type"], "LoadImage")
            self.assertIn("H3Idea2Video/render_test/handoff", first_loader["inputs"]["image"])
            self.assertEqual(len(fake_comfy.uploads), 1)
            self.assertEqual(fake_comfy.uploads[0].name, "tail_native.png")
            self.assertFalse(any(key.startswith("ref_images.") for key in second_hybrid))
            completed = store.load("render_test")
            self.assertEqual(completed["status"], "completed")
            self.assertEqual([item["render_state"] for item in completed["segments"]], ["accepted", "accepted"])
            self.assertEqual(completed["master"]["frames"], completed["target_frames"])
            self.assertEqual(completed["identity_references"], {})
            self.assertEqual(qc_image_counts, [])
            self.assertEqual(provider_calls, [])


class MasterMediaTests(unittest.TestCase):
    def test_cpu_only_master_assembly_preserves_exact_frames_and_audio(self) -> None:
        try:
            import av
        except ImportError:
            self.skipTest("PyAV is not available in this Python environment")

        def make_segment(path: Path) -> None:
            with av.open(str(path), "w") as output:
                video = output.add_stream("libx264", rate=24)
                video.width = 1080
                video.height = 1920
                video.pix_fmt = "yuv420p"
                audio = output.add_stream("aac", rate=48_000)
                audio.layout = "stereo"
                for index in range(2):
                    frame = av.VideoFrame(1080, 1920, "yuv420p")
                    frame.pts = index
                    frame.time_base = Fraction(1, 24)
                    for plane in frame.planes:
                        plane.update(bytes(plane.buffer_size))
                    for packet in video.encode(frame):
                        output.mux(packet)
                for packet in video.encode():
                    output.mux(packet)
                sound = av.AudioFrame(format="fltp", layout="stereo", samples=4_000)
                sound.sample_rate = 48_000
                sound.pts = 0
                sound.time_base = Fraction(1, 48_000)
                for plane in sound.planes:
                    plane.update(bytes(plane.buffer_size))
                for packet in audio.encode(sound):
                    output.mux(packet)
                for packet in audio.encode():
                    output.mux(packet)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "first.mp4", root / "second.mp4"
            make_segment(first)
            make_segment(second)
            result = longform_runtime.assemble_master_video(
                [first, second], root / "master.mp4", expected_frames=4
            )
            self.assertEqual(result["frames"], 4)
            self.assertEqual((result["width"], result["height"]), (1080, 1920))
            self.assertTrue(result["has_audio"])
            self.assertEqual(len(result["sha256"]), 64)


class LongFrontendAndHybridTests(unittest.TestCase):
    def test_local_session_ui_has_no_api_key_field(self) -> None:
        html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        javascript = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('id="longApiKey"', html)
        self.assertNotIn('id="apiKey"', html)
        self.assertNotIn("api_key", javascript)
        self.assertIn('id="lmSessionStartBtn"', html)
        self.assertIn('id="confirmAuthoringBtn"', html)
        self.assertIn("function ensureLMStudioSession", javascript)
        self.assertIn("function releaseLMStudioSession", javascript)

    def test_local_port_settings_are_config_driven_and_never_use_local_storage(self) -> None:
        html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        javascript = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")
        for field_id in (
            "studioPortInput",
            "lmstudioPortInput",
            "comfyuiPortInput",
            "lmstudioAutoStartInput",
            "saveEndpointSettingsBtn",
        ):
            self.assertIn(f'id="{field_id}"', html)
        self.assertIn('postJson("/api/settings/ports"', javascript)
        self.assertIn('postJson("/api/lmstudio/server/start"', javascript)
        self.assertIn("lmstudio_port_occupied", javascript)
        for field_id in ("studioPortInput", "lmstudioPortInput", "comfyuiPortInput"):
            field_line = next(line for line in html.splitlines() if f'id="{field_id}"' in line)
            self.assertNotIn("data-persist", field_line)

    def test_long_story_cards_bind_the_original_single_workspace(self) -> None:
        javascript = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")
        html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        styles = (PROJECT_ROOT / "web" / "style.css").read_text(encoding="utf-8")
        self.assertIn("function renderStoryCardSegments", javascript)
        self.assertIn("data-story-field", javascript)
        self.assertIn("function selectLongSegment", javascript)
        self.assertIn("function saveBoundLongWorkspace", javascript)
        self.assertIn("function generateAllLongPrompts", javascript)
        self.assertIn("function renderLongDeliverables", javascript)
        self.assertIn("function buildAllLongPromptsOutput", javascript)
        self.assertIn("const latestProjectId = result.projects[0].id", javascript)
        self.assertIn('runTimelineOperation("move_to"', javascript)
        self.assertIn('id="longSingleHost"', html)
        self.assertIn('id="generateAllLongPromptsBtn"', html)
        self.assertIn('id="longAutoCompile"', html)
        self.assertIn('id="longFullStoryOutput"', html)
        self.assertIn('id="longPromptOutputs"', html)
        self.assertIn('id="copyAllLongPromptsBtn"', html)
        self.assertIn(".story-drag-handle", styles)
        self.assertIn(".segment-story-target", styles)
        self.assertIn(".long-deliverables", styles)

    def test_hybrid_install_is_scoped_and_refuses_overwrite(self) -> None:
        script = (PROJECT_ROOT / "tools" / "manage_hybrid_node.ps1").read_text(encoding="utf-8")
        self.assertIn("H3PromptStudio_HybridCond", script)
        self.assertIn("Refusing to overwrite existing path", script)
        self.assertIn("not the expected junction", script)
        self.assertNotIn("Remove-Item -Recurse", script)
        upstream = (PROJECT_ROOT / "vendor" / "h3-hybrid-node" / "UPSTREAM.md").read_text(encoding="utf-8")
        self.assertIn("minimax-h3-hybrid-cond", upstream)


@unittest.skipUnless(HAS_COMFY_TORCH, "ComfyUI/Torch integration is not installed")
class ProjectOutputNodeTests(unittest.TestCase):
    def test_image_output_is_marker_gated_atomic_and_receipted(self) -> None:
        self.assertEqual(
            rule_nodes._resolve_project_root("$IDEA2VIDEO_PROJECT_ROOT"),
            PROJECT_ROOT.resolve(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".h3-idea2video-root").write_text("unrelated\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                rule_nodes._resolve_project_root(str(root))
            (root / ".h3-idea2video-root").write_text(
                "MiniMax-H3-Idea2Video-tool\n", encoding="utf-8"
            )
            images = rule_nodes.torch.zeros((2, 3, 4, 3), dtype=rule_nodes.torch.float32)
            result = rule_nodes.H3Idea2VideoProjectImageSave().save(
                images,
                str(root),
                "project-a/segments/0001/attempt_1/qc.png",
            )
            receipt = json.loads(result["result"][0])
            self.assertEqual(receipt["schema"], "h3_idea2video_artifact_receipt_v1")
            self.assertEqual(len(receipt["files"]), 2)
            for item in receipt["files"]:
                path = root / item["relative_path"]
                self.assertTrue(path.is_file())
                self.assertTrue(item["relative_path"].startswith("runs/project-a/"))
                self.assertEqual(item["bytes"], path.stat().st_size)
                self.assertEqual(item["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            with self.assertRaises(ValueError):
                rule_nodes.H3Idea2VideoProjectImageSave().save(
                    images[:1], str(root), "../outside.png"
                )

    def test_file_copy_accepts_only_comfy_output_and_writes_project_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".h3-idea2video-root").write_text(
                "MiniMax-H3-Idea2Video-tool\n", encoding="utf-8"
            )
            comfy_output = root / "comfy-output"
            comfy_output.mkdir()
            source = comfy_output / "native.mp4"
            source.write_bytes(b"native")
            folder_paths = SimpleNamespace(
                get_output_directory=lambda: str(comfy_output)
            )
            with mock.patch.dict(sys.modules, {"folder_paths": folder_paths}):
                result = rule_nodes.H3Idea2VideoProjectFileCopy().copy(
                    str(source),
                    str(root),
                    "project-a/context_loop/output/native.mp4",
                )
                receipt = json.loads(result["result"][0])
                destination = root / receipt["files"][0]["relative_path"]
                self.assertEqual(destination.read_bytes(), b"native")
                outside = root / "outside.mp4"
                outside.write_bytes(b"outside")
                with self.assertRaises(ValueError):
                    rule_nodes.H3Idea2VideoProjectFileCopy().copy(
                        str(outside), str(root), "project-a/outside.mp4"
                    )

    def test_video_output_encodes_h264_crf18_directly_to_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".h3-idea2video-root").write_text(
                "MiniMax-H3-Idea2Video-tool\n", encoding="utf-8"
            )
            captured: dict[str, object] = {}

            class FakeVideo:
                def save_to(self, path, **kwargs):
                    captured.update(kwargs)
                    Path(path).write_bytes(b"mp4")

            comfy_api = ModuleType("comfy_api")
            latest = ModuleType("comfy_api.latest")
            latest.Types = SimpleNamespace(VideoContainer=lambda value: value)
            comfy_api.latest = latest
            with mock.patch.dict(
                sys.modules,
                {"comfy_api": comfy_api, "comfy_api.latest": latest},
            ):
                result = rule_nodes.H3Idea2VideoProjectVideoSave().save(
                    FakeVideo(),
                    str(root),
                    "project-a/segments/0001/final.mp4",
                    18.0,
                )
            receipt = json.loads(result["result"][0])
            output = root / receipt["files"][0]["relative_path"]
            self.assertEqual(output.read_bytes(), b"mp4")
            self.assertEqual(captured["format"], "mp4")
            self.assertEqual(captured["codec"], "h264")
            self.assertEqual(captured["crf"], 18.0)


class WorkflowTests(unittest.TestCase):
    def test_builder_default_source_is_repository_owned_and_reusable(self) -> None:
        source_path = build_workflows.DEFAULT_SOURCE.resolve()
        source_path.relative_to(PROJECT_ROOT.resolve())
        source = json.loads(source_path.read_text(encoding="utf-8"))
        for mode in build_workflows.OUTPUT_FILENAMES:
            rebuilt = build_workflows.build_workflow(source, mode)
            self.assertEqual(build_workflows.validate_workflow(rebuilt, mode), [])

    def _assert_link_integrity(self, workflow: dict[str, object]) -> None:
        top_nodes = {node["id"]: node for node in workflow["nodes"]}
        top_links = {link[0]: link for link in workflow["links"]}
        for link_id, origin_id, origin_slot, target_id, target_slot, _type in workflow["links"]:
            self.assertIn(origin_id, top_nodes)
            self.assertIn(target_id, top_nodes)
            self.assertIn(link_id, top_nodes[origin_id]["outputs"][origin_slot]["links"])
            self.assertEqual(top_nodes[target_id]["inputs"][target_slot]["link"], link_id)
        for node in top_nodes.values():
            for item in node.get("inputs", []):
                if item.get("link") is not None:
                    self.assertIn(item["link"], top_links)
            for item in node.get("outputs", []):
                for link_id in item.get("links") or []:
                    self.assertIn(link_id, top_links)

        subgraph = workflow["definitions"]["subgraphs"][0]
        nodes = {node["id"]: node for node in subgraph["nodes"]}
        links = {link["id"]: link for link in subgraph["links"]}
        for link in links.values():
            if link["origin_id"] != -10:
                self.assertIn(link["origin_id"], nodes)
                self.assertIn(link["id"], nodes[link["origin_id"]]["outputs"][link["origin_slot"]]["links"])
            if link["target_id"] != -20:
                self.assertIn(link["target_id"], nodes)
                self.assertEqual(nodes[link["target_id"]]["inputs"][link["target_slot"]]["link"], link["id"])
        for node in nodes.values():
            for item in node.get("inputs", []):
                if item.get("link") is not None:
                    self.assertIn(item["link"], links)
            for item in node.get("outputs", []):
                for link_id in item.get("links") or []:
                    self.assertIn(link_id, links)

    def test_all_workflows(self) -> None:
        expected_images = {"T2VA": 0, "I2VA": 1, "FL2VA": 2}
        for mode, filename in build_workflows.OUTPUT_FILENAMES.items():
            path = PROJECT_ROOT / "comfyui_workflows" / filename
            workflow = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(build_workflows.validate_workflow(workflow, mode), [])
            self._assert_link_integrity(workflow)
            load_images = [node for node in workflow["nodes"] if node["type"] == "LoadImage"]
            self.assertEqual(len(load_images), expected_images[mode])
            project_save = next(
                node
                for node in workflow["nodes"]
                if node["type"] == "H3Idea2VideoProjectVideoSave"
            )
            self.assertEqual(
                project_save["widgets_values"][0], "$IDEA2VIDEO_PROJECT_ROOT"
            )
            self.assertTrue(
                project_save["widgets_values"][1].startswith("manual_exports/")
            )
            self.assertEqual(project_save["widgets_values"][2], 18.0)
            subgraph = workflow["definitions"]["subgraphs"][0]
            nodes = {node["id"]: node for node in subgraph["nodes"]}
            h3 = next(
                node
                for node in nodes.values()
                if node["type"] == "MiniMaxH3ImageToVideo"
            )
            self.assertEqual(
                next(node for node in nodes.values() if node["type"] == "UNETLoader")["widgets_values"][0],
                "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            )
            self.assertEqual(
                next(node for node in nodes.values() if node["type"] == "MiniMaxH3TurboLoRA")["widgets_values"][0],
                "minimax_h3_turbo_v4_step600_ema.safetensors",
            )
            self.assertEqual(next(node for node in nodes.values() if node["type"] == "BasicScheduler")["widgets_values"][1], 8)
            self.assertEqual(h3["widgets_values"][1:4], [768, 1344, 175])
            output_link = next(
                link
                for link in subgraph["links"]
                if link["target_id"] == -20 and link["target_slot"] == 0
            )
            self.assertEqual(nodes[output_link["origin_id"]]["widgets_values"], [24, 10])
            trim_audio = next(
                node for node in nodes.values() if node["type"] == "TrimAudioDuration"
            )
            self.assertEqual(trim_audio["widgets_values"], [0, 7.0])
            self.assertEqual(workflow["extra"]["h3_prompt_studio"]["output_frames"], 168)
            self.assertEqual(workflow["extra"]["h3_prompt_studio"]["dropped_indices"], [12, 37, 62, 87, 112, 137, 162])
            self.assertNotIn("deepseek", json.dumps(workflow, ensure_ascii=False).lower())
            if mode == "FL2VA":
                titles = {node.get("title", "") for node in nodes.values()}
                self.assertIn("Exact output frame 167 from Picture 2", titles)
                self.assertIn("Append exact Picture 2 as output frame 167", titles)


class ContentReconciliationTests(unittest.TestCase):
    def _runtime(self, root: Path, store: longform.LongProjectStore, provider):
        return longform_runtime.LongFormRuntime(
            store=store,
            project_root=PROJECT_ROOT,
            provider_call=provider,
            segment_precompiler=app.precompile_segment_workspace,
        )

    def test_story_edit_preserves_opening_and_explicit_sync_derives_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = longform.LongProjectStore(root / "runs")
            project = make_story_project("story_reconcile", 11)
            make_context_spec(project)
            first = project["segments"][0]
            first["single_workspace"]["script"]["shots"][0]["dialogue"] = [
                {"speaker": "甲", "language": "Chinese", "text": "原句不能改。"}
            ]
            longform._materialize_workspace(first)
            first["content_sync"] = longform.make_content_sync(
                first, state="clean", source="test"
            )
            project = store.save(project)
            opening = project["segments"][0]["story_card"]["opening_state"]
            edited = longform.edit_story_card(
                store,
                project,
                "seg_0001",
                {"story_text": "用户重写后的中文剧情正文。", "opening_state": "不得采用"},
                confirm_invalidate=True,
            )
            self.assertEqual(edited["segments"][0]["content_sync"]["state"], "story_dirty")
            self.assertEqual(edited["segments"][0]["story_card"]["opening_state"], opening)
            self.assertEqual(edited["segments"][1]["content_sync"]["state"], "story_dirty")

            def provider(messages, _settings, event_callback=None):
                request = json.loads(messages[-1]["content"])
                self.assertEqual(request["source_of_change"], "story_dirty")
                self.assertEqual(request["fixed_opening_state"], opening)
                return {
                    "title": "同步后的标题",
                    "story_text": "模型不得覆盖这句",
                    "ending_state": "主体在画面右侧停稳，灯光保持柔和。",
                    "present_characters": ["甲"],
                    "recommended_boundary_before": "start",
                    "boundary_reason": "第一段固定为开始。",
                    "continuity_compatible": True,
                    "conflict_message": "",
                    "dialogue": [{"text": "模型伪造对白"}],
                    "warnings": [],
                }, {"total_tokens": 9}, "stop"

            runtime = self._runtime(root, store, provider)
            started = runtime.start_reconcile("story_reconcile", "seg_0001", object())
            task = wait_background_task(runtime, started["task"]["id"])
            self.assertEqual(task["state"], "completed", task)
            synced = store.load("story_reconcile")
            segment = synced["segments"][0]
            self.assertEqual(segment["story_card"]["story_text"], "用户重写后的中文剧情正文。")
            self.assertEqual(segment["story_card"]["opening_state"], opening)
            self.assertEqual(
                segment["story_card"]["dialogue"],
                [{"speaker": "甲", "language": "Chinese", "text": "原句不能改。"}],
            )
            self.assertEqual(segment["content_sync"]["state"], "clean")
            self.assertEqual(segment["prompt_state"], "stale")
            self.assertEqual(segment["h3_prompt"], "")
            self.assertEqual(synced["usage"]["total_tokens"], 9)
            self.assertEqual(synced["current_revision"], edited["current_revision"] + 1)

    def test_shot_edit_requires_boundary_confirmation_and_preserves_script(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = longform.LongProjectStore(root / "runs")
            project = make_story_project("shot_reconcile", 17)
            make_context_spec(project)
            project = store.save(project)
            target = project["segments"][1]
            opening = target["story_card"]["opening_state"]
            workspace = deepcopy(target["single_workspace"])
            workspace["script"]["shots"][0]["action"] = "用户手动改成主体突然进入另一处空间。"
            workspace["script"]["shots"][0]["dialogue"] = [
                {"speaker": "甲", "language": "Chinese", "text": "逐字保留这一句。"}
            ]
            dirty = longform.save_segment_workspace(
                store,
                project,
                target["id"],
                workspace,
                expected_revision=0,
            )
            self.assertEqual(dirty["segments"][1]["content_sync"]["state"], "shots_dirty")
            self.assertEqual(dirty["segments"][1]["h3_prompt"], "")
            original_script = deepcopy(dirty["segments"][1]["single_workspace"]["script"])

            def provider(messages, _settings, event_callback=None):
                calls.append("reconcile")
                return {
                    "title": "切到另一处空间",
                    "story_text": "主体从既有开场状态切入另一处空间并完成动作。",
                    "ending_state": "主体站在新空间中央，动作结束。",
                    "present_characters": ["甲"],
                    "recommended_boundary_before": "cut",
                    "boundary_reason": "Shot 1 的空间与上一段真实尾帧无法连续解释。",
                    "continuity_compatible": False,
                    "conflict_message": "保留 continuous 会造成空间跳变。",
                    "warnings": [],
                }, {}, "stop"

            runtime = self._runtime(root, store, provider)
            started = runtime.start_reconcile("shot_reconcile", target["id"], object())
            task = wait_background_task(runtime, started["task"]["id"])
            self.assertTrue(task["result"]["requires_boundary_confirmation"])
            still_dirty = store.load("shot_reconcile")
            self.assertEqual(still_dirty["segments"][1]["boundary_before"], "continuous")
            committed = runtime.commit_reconcile_proposal(
                task["result"]["proposal_id"], accept_boundary=True
            )
            segment = committed["segments"][1]
            self.assertEqual(segment["boundary_before"], "cut")
            self.assertEqual(segment["story_card"]["opening_state"], opening)
            self.assertEqual(segment["single_workspace"]["script"], original_script)
            self.assertTrue(segment["single_workspace"]["preserve_script_on_precompile"])
            self.assertEqual(
                segment["story_card"]["dialogue"][0]["text"],
                "逐字保留这一句。",
            )
            self.assertEqual(segment["content_sync"]["state"], "clean")
            self.assertEqual(committed["segments"][2]["content_sync"]["state"], "story_dirty")
            self.assertEqual(calls, ["reconcile"])


class ContextLoopTests(unittest.TestCase):
    @unittest.skipUnless(HAS_COMFY_TORCH, "ComfyUI/Torch integration is not installed")
    def test_rule_adapter_maps_exact_frames_audio_and_native_endpoint(self) -> None:
        project = make_story_project(project_id="adapter_exact", seconds=11)
        spec = make_context_spec(project)
        plan = build_context_workflow.plugin_plan(spec)
        parsed, _summary, count, width, height = rule_nodes.H3PromptStudioRulePlan().build(
            json.dumps(plan, ensure_ascii=False)
        )
        self.assertEqual(count, len(project["segments"]))
        self.assertEqual((width, height), (768, 1344))
        shot = parsed["shots"][1]
        raw_frames = int(shot["raw_frames"])
        target_frames = int(shot["target_frames"])
        state = {
            "plan": parsed,
            "index": 2,
            "previous_frames": __import__("torch").zeros((1, 2, 2, 3)),
        }
        torch = __import__("torch")
        images = torch.arange(raw_frames, dtype=torch.float32).reshape(raw_frames, 1, 1, 1)
        images = images.repeat(1, 1, 1, 3)
        sample_rate = 24000
        native_samples = round(raw_frames / 24 * sample_rate)
        audio = {
            "waveform": torch.arange(
                native_samples + 8, dtype=torch.float32
            ).reshape(1, 1, -1),
            "sample_rate": sample_rate,
        }
        delivered, delivered_audio, _status = rule_nodes.H3PromptStudioExactFrameMap().map(
            state, images, audio
        )
        self.assertEqual(int(delivered.shape[0]), target_frames)
        self.assertEqual(float(delivered[0, 0, 0, 0]), 1.0)
        self.assertEqual(float(delivered[-1, 0, 0, 0]), float(raw_frames - 1))
        self.assertEqual(
            int(delivered_audio["waveform"].shape[-1]),
            round(target_frames / 24 * sample_rate),
        )
        self.assertEqual(
            float(delivered_audio["waveform"][0, 0, 0]),
            float(round(sample_rate / 24)),
        )
        self.assertEqual(
            float(delivered_audio["waveform"][0, 0, -1]),
            float(native_samples - 1),
        )
        routed_first, routed_last, _ = rule_nodes.H3PromptStudioKeyframeRouter().route(state)
        self.assertEqual(tuple(routed_first.shape), (1, 1344, 768, 3))
        self.assertIsNone(routed_last)

    def test_rule_runtime_has_no_second_llm_compiler_surface(self) -> None:
        self.assertFalse(hasattr(context_runtime.ContextLoopRuntime, "_provider"))
        self.assertFalse(hasattr(context_runtime.ContextLoopRuntime, "_generate_one_scene"))
        self.assertFalse(
            hasattr(context_runtime.ContextLoopRuntime, "_start_generation_legacy_disabled")
        )

    def test_pinned_nodes_installer_manages_only_verified_junctions(self) -> None:
        upstream = (
            PROJECT_ROOT / "vendor" / "minimax-h3-contex-loop" / "UPSTREAM.md"
        ).read_text(encoding="utf-8")
        self.assertIn("0.3.20", upstream)
        self.assertIn("81e615c66384e8f747ded5d181ef5807f2775daa", upstream)
        self.assertTrue(
            (PROJECT_ROOT / "vendor" / "minimax-h3-contex-loop" / "LICENSE").is_file()
        )
        script = PROJECT_ROOT / "tools" / "manage_context_loop_node.ps1"
        content = script.read_text(encoding="utf-8")
        self.assertIn("ComfyUI-MiniMaxH3-Contex-Loop", content)
        self.assertIn("Refusing to overwrite existing path", content)
        self.assertIn("Split-Path -Leaf $Selected", content)
        self.assertIn("Rolled back junction created by this run", content)
        self.assertNotIn("Remove-Item -Recurse", content)
        uninstall_bat = (
            PROJECT_ROOT / "uninstall_comfyui_nodes.bat"
        ).read_text(encoding="utf-8")
        self.assertIn("manage_context_loop_node.ps1", uninstall_bat)
        self.assertIn("-Action Uninstall", uninstall_bat)
        if os.name != "nt":
            return
        with tempfile.TemporaryDirectory() as directory:
            custom_nodes = Path(directory) / "custom_nodes"
            custom_nodes.mkdir()
            command = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-ProjectRoot",
                str(PROJECT_ROOT),
                "-CustomNodesRoot",
                str(custom_nodes),
            ]
            try:
                installed = subprocess.run(
                    command + ["-Action", "Install"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(installed.returncode, 0, installed.stderr)
                status = subprocess.run(
                    command + ["-Action", "Status"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertIn("Context Loop 0.3.20", status.stdout)
                duplicate = subprocess.run(
                    command + ["-Action", "Install"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
                self.assertIn("ALREADY_INSTALLED", duplicate.stdout)
                self.assertTrue(
                    (custom_nodes / "H3PromptStudioRuleAdapter").exists()
                )
                uninstalled = subprocess.run(
                    command + ["-Action", "Uninstall"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
                self.assertIn("UNINSTALLED", uninstalled.stdout)
                self.assertFalse(
                    (custom_nodes / "ComfyUI-MiniMaxH3-Contex-Loop").exists()
                )
                self.assertFalse(
                    (custom_nodes / "H3PromptStudioRuleAdapter").exists()
                )
                self.assertFalse(
                    (custom_nodes / "H3PromptStudio_HybridCond").exists()
                )

                conflict = custom_nodes / "H3PromptStudioRuleAdapter"
                conflict.mkdir()
                refused = subprocess.run(
                    command + ["-Action", "Install"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("Refusing to overwrite existing path", refused.stderr)
                self.assertFalse(
                    (custom_nodes / "ComfyUI-MiniMaxH3-Contex-Loop").exists()
                )
                conflict.rmdir()
            finally:
                subprocess.run(
                    command + ["-Action", "Uninstall"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )

    def test_duration_calibration_preserves_context_without_duplicate_frames(self) -> None:
        first = context_loop.calibrate_scene_duration(5.0, scene_index=1)
        later = context_loop.calibrate_scene_duration(5.0, scene_index=2)
        final_short = context_loop.calibrate_scene_duration(4.2, scene_index=3)

        self.assertEqual(first["raw_frames"], 124)
        self.assertEqual(first["delivered_frames"], 124)
        self.assertEqual(later["raw_frames"], 124)
        self.assertEqual(later["delivered_frames"], 123)
        self.assertEqual(later["raw_frames"] - later["delivered_frames"], 1)
        self.assertEqual(final_short["actual_seconds"], later["actual_seconds"])
        self.assertEqual(
            longform.h3_model_frames(101, continuous=True),
            124,
        )
        for timing in (first, later, final_short):
            self.assertEqual(timing["raw_frames"] % 17, 5)

    def test_base_and_ref_prompts_enforce_opening_and_exact_dialogue(self) -> None:
        project = make_story_project(seconds=11)
        project["segments"][0]["dialogue"] = [
            {"language": "Chinese", "text": "这句对白必须原样保留。"}
        ]
        project["segments"][0]["visible_text"] = ["原样字幕"]

        base = context_prompt(
            scene_index=1,
            opening=True,
            dialogue=project["segments"][0]["dialogue"],
            visible_text=project["segments"][0]["visible_text"],
        )
        self.assertEqual(
            context_loop.validate_scene_prompt(
                base,
                segment=project["segments"][0],
                scene_index=1,
                actual_seconds=5.2,
                identity_count=0,
                has_opening_image=True,
            ),
            [],
        )
        self.assertTrue(base.startswith(context_loop.I2VA_LINE + "\n\n"))
        self.assertIn("<d>[Chinese] 这句对白必须原样保留。</d>", base)

        later_with_picture = context_prompt(scene_index=2).replace(
            "[Shot 1]", "[Shot 1] <Picture 1>"
        )
        errors = context_loop.validate_scene_prompt(
            later_with_picture,
            segment=project["segments"][1],
            scene_index=2,
            actual_seconds=5.2,
            identity_count=0,
            has_opening_image=True,
        )
        self.assertTrue(any("Picture" in item for item in errors))

        ref_first = context_prompt(
            scene_index=1,
            identity_count=2,
            opening=True,
            dialogue=project["segments"][0]["dialogue"],
            visible_text=project["segments"][0]["visible_text"],
        )
        ref_later = context_prompt(scene_index=2, identity_count=2, opening=True)
        self.assertEqual(
            context_loop.validate_scene_prompt(
                ref_first,
                segment=project["segments"][0],
                scene_index=1,
                actual_seconds=5.2,
                identity_count=2,
                has_opening_image=True,
            ),
            [],
        )
        self.assertIn("<Picture 3>", ref_first)
        self.assertNotIn("<Picture 3>", ref_later)

        invalid_zero = ref_later.replace(
            "detailed_description:\n",
            "detailed_description:\n<Picture 0> and <Subject 0> are invalid. ",
        )
        zero_errors = context_loop.validate_scene_prompt(
            invalid_zero,
            segment=project["segments"][1],
            scene_index=2,
            actual_seconds=5.2,
            identity_count=2,
            has_opening_image=True,
        )
        self.assertTrue(any("Picture" in item for item in zero_errors))
        self.assertTrue(any("Subject" in item for item in zero_errors))

        clock_project = make_story_project(seconds=11)
        clock_project["segments"][0]["visible_text"] = ["20:30"]
        clock_prompt = context_prompt(scene_index=1, visible_text=["20:30"])
        self.assertEqual(
            context_loop.validate_scene_prompt(
                clock_prompt,
                segment=clock_project["segments"][0],
                scene_index=1,
                actual_seconds=5.2,
                identity_count=0,
                has_opening_image=False,
            ),
            [],
        )

    def test_sidecar_is_atomic_revisioned_editable_and_stale_aware(self) -> None:
        project = make_story_project(project_id="context_atomic", seconds=11)
        spec = make_context_spec(project)
        with tempfile.TemporaryDirectory() as directory:
            store = longform.LongProjectStore(Path(directory) / "runs")
            store.save(project)
            sidecar = context_loop.ContextLoopStore(store)
            saved = sidecar.save(spec, expected_revision=0, reason="offline fixture")
            self.assertEqual(saved["revision"], 1)
            self.assertTrue(sidecar.path(project["id"]).is_file())

            saved2 = sidecar.save(saved, expected_revision=1, reason="rule rebuild")
            self.assertEqual(saved2["revision"], 2)
            self.assertTrue(
                (sidecar.root(project["id"]) / "revisions" / "000001.json").is_file()
            )
            with self.assertRaisesRegex(longform.LongFormError, "") as conflict:
                sidecar.save(saved2, expected_revision=1)
            self.assertEqual(conflict.exception.code, "context_revision_conflict")

            changed_project = deepcopy(project)
            changed_project["segments"][1]["story_card"]["story_text"] += " changed"
            stale = context_loop.assess_staleness(saved2, changed_project)
            self.assertTrue(stale["stale"])
            self.assertEqual(stale["stale_from"], 2)

            legacy = deepcopy(saved2)
            legacy["schema_version"] = 1
            stale_legacy = context_loop.assess_staleness(legacy, project)
            self.assertTrue(stale_legacy["stale"])
            self.assertEqual(stale_legacy["stale_from"], 1)
            self.assertEqual(stale_legacy["status"], "stale")

    def test_context_workflow_is_locked_importable_and_unattended(self) -> None:
        project = make_story_project(project_id="context_workflow", seconds=17)
        spec = make_context_spec(project)
        api_workflow = build_context_workflow.build_api_workflow(spec)
        self.assertEqual(build_context_workflow.validate_api_workflow(api_workflow), [])
        classes = [
            item["class_type"] for item in api_workflow["prompt"].values()
        ]
        self.assertNotIn("MiniMaxH3ChainReview", classes)
        self.assertIn("H3PromptStudioKeyframeRouter", classes)
        self.assertIn("H3PromptStudioExactFrameMap", classes)
        self.assertNotIn("MiniMaxH3ChainContext", classes)
        self.assertNotIn("MiniMaxH3ChainScenePromptEditor", classes)
        plan_node = next(
            item
            for item in api_workflow["prompt"].values()
            if item["class_type"] == "H3PromptStudioRulePlan"
        )
        hybrid = next(
            item
            for item in api_workflow["prompt"].values()
            if item["class_type"] == "MiniMaxH3HybridRefAndKeyframe"
        )
        self.assertFalse(hybrid["inputs"]["also_ref_first_frame"])
        self.assertEqual(hybrid["inputs"]["first_frame"], ["20", 0])
        self.assertEqual(hybrid["inputs"]["last_frame"], ["20", 1])
        plugin_plan = json.loads(plan_node["inputs"]["plan_json"])
        self.assertEqual(plugin_plan["compatibility"]["context_length"], 1)
        self.assertEqual(plugin_plan["compatibility"]["audio_context_length"], 1)
        self.assertEqual(plugin_plan["compatibility"]["audio_mode"], "generated_audio")
        self.assertEqual(len(plugin_plan["shots"]), len(project["segments"]))
        self.assertTrue(all(item["raw_frames"] % 17 == 5 for item in plugin_plan["shots"]))
        self.assertTrue(all(item["steps"] == 8 for item in plugin_plan["shots"]))
        self.assertTrue(
            all(item["prompt"] == item["scene_prompt"] for item in plugin_plan["shots"])
        )
        self.assertEqual(api_workflow["meta"]["provider_calls"], 0)

        ui_workflow = build_context_workflow.build_ui_workflow(api_workflow)
        self.assertEqual(build_context_workflow.validate_ui_workflow(ui_workflow), [])
        self.assertEqual(
            len({int(node["id"]) for node in ui_workflow["nodes"]}),
            len(ui_workflow["nodes"]),
        )

        upscale = build_context_workflow.build_upscale_api_workflow(
            source_video="h3_chains/example/segments/clip_0001.mp4 [output]",
            source_audio="h3_chains/example/generated_audio/clip_0001.wav [output]",
            project_id="context_workflow",
            relative_path="context_workflow/context_loop/output/clip_0001.mp4",
        )
        self.assertEqual(
            build_context_workflow.validate_upscale_api_workflow(upscale), []
        )
        self.assertEqual(upscale["meta"]["resolution"], [1080, 1920])
        self.assertEqual(upscale["meta"]["crf"], 18)

    def test_context_runtime_generates_and_persists_every_complete_prompt(self) -> None:
        calls: list[str] = []

        def provider(*_args, **_kwargs):
            calls.append("unexpected")
            raise AssertionError("rule compilation must not call a provider")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = longform.LongProjectStore(root / "runs")
            project = make_story_project(project_id="context_runtime", seconds=17)
            make_context_spec(project)
            store.save(project)
            runtime = context_runtime.ContextLoopRuntime(
                store=store,
                project_root=PROJECT_ROOT,
                provider_call=provider,
            )
            self.assertFalse(hasattr(runtime, "provider_call"))
            result = runtime.start_generation(
                project["id"], {"base_seed": 1234}, None
            )
            task = wait_background_task(runtime, result["task"]["id"])
            self.assertEqual(task["state"], "completed")
            self.assertEqual(calls, [])
            self.assertEqual(task["result"]["provider_calls"], 0)
            spec = runtime.sidecars.load(project["id"])
            self.assertEqual(spec["status"], "valid")
            self.assertEqual(len(spec["scenes"]), 3)
            self.assertEqual(spec["usage"], {})
            self.assertEqual(
                [scene["prompt"] for scene in spec["scenes"]],
                [segment["h3_prompt"] for segment in project["segments"]],
            )
            self.assertTrue(runtime.artifact_path(project["id"], "workflow").is_file())
            self.assertFalse(spec["render"]["native_path"])

    def test_rule_compilation_blocks_a_missing_or_stale_prompt_without_api(self) -> None:
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = longform.LongProjectStore(root / "runs")
            project = make_story_project(project_id="context_repair", seconds=11)
            make_context_spec(project)
            project["segments"][0]["h3_prompt"] = ""
            project["segments"][0]["prompt_state"] = "stale"
            project["segments"][0]["single_workspace"]["state"] = "stale"
            store.save(project)
            runtime = context_runtime.ContextLoopRuntime(
                store=store,
                project_root=PROJECT_ROOT,
                provider_call=lambda *_args, **_kwargs: calls.append("unexpected"),
            )
            result = runtime.start_generation(
                project["id"], {}, None
            )
            task = wait_background_task(runtime, result["task"]["id"])
            self.assertEqual(task["state"], "failed")
            self.assertEqual(task["error"]["code"], "context_prompt_not_ready")
            self.assertEqual(calls, [])

    def test_rule_compilation_blocks_workspace_prompt_drift(self) -> None:
        project = make_story_project(project_id="context_prompt_drift", seconds=11)
        make_context_spec(project)
        project["segments"][0]["single_workspace"]["prompt"] += "\nDRIFT"
        with self.assertRaises(longform.LongFormError) as caught:
            context_loop.build_rule_spec(project, base_seed=17)
        self.assertEqual(caught.exception.code, "context_prompt_source_mismatch")

    def test_rule_compilation_blocks_unsynchronized_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = longform.LongProjectStore(root / "runs")
            project = make_story_project(project_id="context_pause", seconds=17)
            make_context_spec(project)
            longform.mark_content_dirty(
                project["segments"][1], "story_dirty", source="manual_story_edit"
            )
            store.save(project)
            runtime = context_runtime.ContextLoopRuntime(
                store=store,
                project_root=PROJECT_ROOT,
                provider_call=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("provider must not be called")
                ),
            )
            started = runtime.start_generation(
                project["id"], {}, None
            )
            task = wait_background_task(runtime, started["task"]["id"])
            self.assertEqual(task["state"], "failed")
            self.assertEqual(task["error"]["code"], "context_content_sync_required")

    def test_rule_compilation_detects_clean_state_hash_drift(self) -> None:
        project = make_story_project(project_id="context_hash_drift", seconds=11)
        make_context_spec(project)
        project["segments"][0]["story_card"]["story_text"] += "（外部修改）"
        self.assertEqual(project["segments"][0]["content_sync"]["state"], "clean")
        self.assertFalse(longform.content_sync_is_current(project["segments"][0]))
        migrated, changed = longform.migrate_project(project)
        self.assertTrue(changed)
        self.assertEqual(migrated["segments"][0]["content_sync"]["state"], "story_dirty")
        with self.assertRaises(longform.LongFormError) as caught:
            context_loop.build_rule_spec(project, base_seed=17)
        self.assertEqual(caught.exception.code, "context_content_sync_required")

    def test_context_render_mock_runs_recursive_native_then_optional_upscale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_root = root / "output"
            store = longform.LongProjectStore(root / "runs")
            project = make_story_project(project_id="context_render_mock", seconds=11)
            spec = make_context_spec(project)
            store.save(project)
            spec["outputs"]["upscale_1080"] = True
            histories: dict[str, dict[str, object]] = {}
            viewed: dict[str, bytes] = {}
            submitted_context: list[dict[str, object]] = []
            submitted_upscale: list[dict[str, object]] = []

            class FakeContextClient:
                def preflight_context(self, *, upscale=False):
                    self.upscale = upscale
                    return {"ok": True}

                def wait_until_idle(self, *, stop):
                    self.assert_not_stopped = not stop()

                def submit_context(self, workflow):
                    submitted_context.append(workflow)
                    run_name = workflow["meta"]["run_name"]
                    plugin_shots = json.loads(
                        next(
                            item
                            for item in workflow["prompt"].values()
                            if item["class_type"] == "H3PromptStudioRulePlan"
                        )["inputs"]["plan_json"]
                    )["shots"]
                    for index, shot in enumerate(plugin_shots, start=1):
                        video_name = f"h3_chains/{run_name}/segments/clip_{index:04d}.mp4"
                        audio_name = f"h3_chains/{run_name}/generated_audio/clip_{index:04d}.wav"
                        video = output_root / video_name
                        audio = output_root / audio_name
                        video.parent.mkdir(parents=True, exist_ok=True)
                        audio.parent.mkdir(parents=True, exist_ok=True)
                        video.write_bytes(f"native-{index}".encode())
                        audio.write_bytes(f"audio-{index}".encode())
                        metadata = {
                            "segment": {
                                "index": index,
                                "id": shot["id"],
                                "segment": video_name,
                                "generated_audio": audio_name,
                                "segment_sha256": hashlib.sha256(
                                    video.read_bytes()
                                ).hexdigest(),
                                "generated_audio_sha256": hashlib.sha256(
                                    audio.read_bytes()
                                ).hexdigest(),
                            }
                        }
                        viewed[
                            f"h3_chains/{run_name}/checkpoints/clip_{index:04d}.json"
                        ] = json.dumps(metadata).encode("utf-8")
                    output_node = workflow["meta"]["output_nodes"]["project_native"]
                    relative = workflow["prompt"][output_node]["inputs"]["relative_path"]
                    master = root / "runs" / relative
                    master.parent.mkdir(parents=True, exist_ok=True)
                    master.write_bytes(b"native-master")
                    prompt_id = "native_prompt"
                    histories[prompt_id] = {
                        "outputs": {
                            output_node: {
                                "text": [artifact_receipt(root, [master], "copy")]
                            }
                        }
                    }
                    return prompt_id

                def submit_upscale(self, workflow):
                    submitted_upscale.append(workflow)
                    index = len(submitted_upscale)
                    output_node = workflow["meta"]["output_nodes"]["video"]
                    relative = workflow["prompt"][output_node]["inputs"]["relative_path"]
                    target = root / "runs" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(f"upscale-{index}".encode())
                    prompt_id = f"upscale_{index}"
                    histories[prompt_id] = {
                        "outputs": {
                            output_node: {
                                "text": [artifact_receipt(root, [target], "video")]
                            }
                        }
                    }
                    return prompt_id

                def wait_history(self, prompt_id, *, stop):
                    return histories[prompt_id]

                def view_bytes(self, *, name, file_type="output"):
                    self.assert_output = file_type == "output"
                    return viewed[name]

            inspected: list[tuple[int, tuple[int, int]]] = []

            def inspect(_path, *, expected_frames, expected_size, require_audio):
                inspected.append((expected_frames, expected_size))
                return {
                    "frames": expected_frames,
                    "width": expected_size[0],
                    "height": expected_size[1],
                    "has_audio": require_audio,
                }

            def assemble(paths, destination, *, expected_frames):
                self.assertEqual(len(paths), len(project["segments"]))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"upscaled-master")
                return {"frames": expected_frames, "width": 1080, "height": 1920}

            runtime = context_runtime.ContextLoopRuntime(
                store=store,
                project_root=PROJECT_ROOT,
                provider_call=lambda *_args, **_kwargs: ({}, {}, "stop"),
                comfy_client_factory=FakeContextClient,
                media_inspector=inspect,
                master_assembler=assemble,
            )
            saved = runtime.sidecars.save(spec, expected_revision=0, reason="render fixture")
            runtime._write_artifacts(saved)
            result = runtime.start_render(project["id"], {"start_scene": 1})
            task = wait_background_task(runtime, result["task"]["id"])
            self.assertEqual(task["state"], "completed")
            self.assertEqual(len(submitted_context), 1)
            self.assertEqual(len(submitted_upscale), len(project["segments"]))
            classes = {
                node["class_type"] for node in submitted_context[0]["prompt"].values()
            }
            self.assertNotIn("MiniMaxH3ChainReview", classes)
            completed = runtime.sidecars.load(project["id"])
            self.assertTrue(Path(completed["render"]["native_path"]).is_file())
            self.assertTrue(Path(completed["render"]["upscaled_path"]).is_file())
            self.assertEqual(completed["render"]["state"], "completed")
            self.assertIn((completed["total_delivered_frames"], (768, 1344)), inspected)
            self.assertEqual(
                sum(1 for _frames, size in inspected if size == (1080, 1920)),
                len(project["segments"]),
            )


class BrowserLaunchTests(unittest.TestCase):
    def test_windows_server_binding_is_exclusive(self) -> None:
        if os.name == "nt":
            self.assertFalse(app.StudioHTTPServer.allow_reuse_address)

    def test_start_bat_enables_browser_launch(self) -> None:
        content = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")
        self.assertIn('"%PROJECT_DIR%app.py" --open-browser', content)
        self.assertNotIn("--port 8794", content)
        self.assertIn("ports from config.json", content)
        self.assertEqual(app.browser_url("::1", 8794), "http://[::1]:8794")

    def test_existing_service_probe_and_browser_helpers_without_gui(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(
            {
                "ok": True,
                "service": "MiniMax H3 Prompt Studio",
                "build_id": app.APP_BUILD_ID,
                "long_api_version": app.LONG_API_VERSION,
                "project_schema_version": longform.SCHEMA_VERSION,
            }
        ).encode("utf-8")
        self.assertTrue(
            app.studio_is_running(
                "http://127.0.0.1:8794",
                urlopen_func=lambda *_args, **_kwargs: response,
            )
        )

        opened: list[tuple[str, int]] = []
        timer = app.schedule_browser_open(
            "http://127.0.0.1:8794",
            delay=0,
            opener=lambda url, new: opened.append((url, new)) or True,
        )
        timer.join(timeout=2)
        self.assertEqual(opened, [("http://127.0.0.1:8794", 2)])


class HealthTests(unittest.TestCase):
    def test_server_refuses_non_loopback_bind(self) -> None:
        with self.assertRaises(app.StudioError) as rejected:
            app.create_server("0.0.0.0", 0)
        self.assertEqual(rejected.exception.code, "studio_host_not_loopback")

    def test_http_guard_rejects_rebinding_cross_origin_and_simple_posts(self) -> None:
        self.assertIsNone(app.StudioHandler._loopback_hostname("evil@127.0.0.1:8794"))
        self.assertIsNone(app.StudioHandler._loopback_hostname("127.0.0.1:not-a-port"))
        with tempfile.TemporaryDirectory() as temporary:
            server = app.create_server(
                "127.0.0.1", 0, runs_root=Path(temporary) / "runs"
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                base = f"http://{host}:{port}"

                connection = http.client.HTTPConnection(host, port, timeout=5)
                connection.request("GET", "/api/health", headers={"Host": "evil.example"})
                rejected_host = connection.getresponse()
                rejected_host_body = json.loads(rejected_host.read())
                connection.close()
                self.assertEqual(rejected_host.status, 403)
                self.assertEqual(
                    rejected_host_body["error"]["code"],
                    "local_request_required",
                )

                cross_origin = urllib.request.Request(
                    base + "/api/lmstudio/session/release",
                    data=b"{}",
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://evil.example",
                    },
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected_origin:
                    urllib.request.urlopen(cross_origin, timeout=5)
                self.assertEqual(rejected_origin.exception.code, 403)
                try:
                    self.assertEqual(
                        json.loads(rejected_origin.exception.read())["error"]["code"],
                        "cross_origin_forbidden",
                    )
                finally:
                    rejected_origin.exception.close()

                simple_post = urllib.request.Request(
                    base + "/api/lmstudio/session/release",
                    data=b"{}",
                    method="POST",
                    headers={"Content-Type": "text/plain"},
                )
                with self.assertRaises(urllib.error.HTTPError) as rejected_type:
                    urllib.request.urlopen(simple_post, timeout=5)
                self.assertEqual(rejected_type.exception.code, 415)
                try:
                    self.assertEqual(
                        json.loads(rejected_type.exception.read())["error"]["code"],
                        "json_content_type_required",
                    )
                finally:
                    rejected_type.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_local_health_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server = app.create_server(
                "127.0.0.1", 0, runs_root=Path(temporary) / "runs"
            )
            project = make_story_project(project_id="health_asset", seconds=5)
            server.long_store.save(project)
            payload = b"\x89PNG\r\n\x1a\nhealth"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                with urllib.request.urlopen(
                    f"http://{host}:{port}/api/health", timeout=5
                ) as response:
                    body = json.loads(response.read().decode("utf-8"))
                self.assertTrue(body["ok"])
                self.assertEqual(body["provider"], "lmstudio")
                self.assertEqual(body["model"], lmstudio_runtime.DEFAULT_MODEL)
                self.assertEqual(body["build_id"], app.APP_BUILD_ID)
                self.assertEqual(body["long_api_version"], app.LONG_API_VERSION)
                self.assertEqual(body["project_schema_version"], longform.SCHEMA_VERSION)
                with urllib.request.urlopen(
                    f"http://{host}:{port}/api/long/projects", timeout=5
                ) as response:
                    long_projects = json.loads(response.read().decode("utf-8"))
                self.assertIn("projects", long_projects)
                upload_request = urllib.request.Request(
                    f"http://{host}:{port}/api/long/projects/health_asset/assets",
                    data=json.dumps(
                        {
                            "name": "health.png",
                            "data_url": "data:image/png;base64,"
                            + base64.b64encode(payload).decode("ascii"),
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(upload_request, timeout=5) as response:
                    uploaded = json.loads(response.read().decode("utf-8"))
                asset = uploaded["asset"]
                asset_url = (
                    f"http://{host}:{port}/api/long/projects/health_asset/assets/"
                    + asset["asset_id"]
                )
                with urllib.request.urlopen(asset_url, timeout=5) as response:
                    self.assertEqual(response.read(), payload)
                    self.assertEqual(response.headers.get_content_type(), "image/png")
                with self.assertRaises(urllib.error.HTTPError) as removed:
                    urllib.request.urlopen(
                        f"http://{host}:{port}/api/input-images", timeout=5
                    )
                self.assertEqual(removed.exception.code, 404)
                removed.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_port_settings_endpoint_persists_utf8_config_and_requires_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(app.load_config(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            server = app.create_server(
                "127.0.0.1",
                0,
                config_path=config_path,
                runs_root=root / "runs",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address
                payload = json.dumps(
                    {
                        "studio_port": 19794,
                        "lmstudio_port": 19234,
                        "comfyui_port": 19188,
                        "lmstudio_auto_start": False,
                    }
                ).encode("utf-8")
                request = urllib.request.Request(
                    f"http://{host}:{port}/api/settings/ports",
                    data=payload,
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = json.loads(response.read().decode("utf-8"))
                self.assertTrue(body["ok"])
                self.assertTrue(body["config"]["restart_required"])
                self.assertEqual(body["config"]["ports"]["configured"]["comfyui"], 19188)
                self.assertEqual(body["config"]["ports"]["active"]["comfyui"], 8188)
                saved = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["studio_port"], 19794)
                self.assertFalse(saved["lmstudio_auto_start"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
