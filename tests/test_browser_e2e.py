#!/usr/bin/env python3
"""Headless browser regression for durable H3 prompts and rule compilation.

The test injects a local fake Qwen provider and LM Studio lifecycle. It never contacts LM Studio or
ComfyUI. Rule-workflow compilation must add zero provider calls, and the test
deliberately never clicks the GPU render button.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

import app  # noqa: E402
import longform  # noqa: E402
from test_offline import fake_single_stage_provider, make_story_project  # noqa: E402


provider_tasks: list[str] = []


class BrowserLMManager:
    def __init__(self) -> None:
        self.loaded = False

    def status(self):
        return {
            "cli": {"available": True, "path": "offline-fake-lms.exe"},
            "server": {"running": True, "base_url": "http://127.0.0.1:1234/api/v1"},
            "model": {
                "key": app.DEFAULT_MODEL,
                "identifier": app.DEFAULT_IDENTIFIER,
                "installed": True,
                "owned_loaded": self.loaded,
                "external_conflicts": [],
                "identifier_conflict": None,
                "context_length": 131072,
            },
            "active_requests": 0,
            "last_error": None,
        }

    def ensure_ready(self):
        self.loaded = True
        return self.status()

    def release(self):
        self.loaded = False
        return self.status()

    def assert_owned_unloaded(self):
        if self.loaded:
            raise AssertionError("browser test render must remain gated")
        return self.status()


def browser_provider(messages, settings, event_callback=None):
    try:
        payload = json.loads(messages[-1]["content"])
    except (KeyError, TypeError, json.JSONDecodeError):
        payload = {}
    provider_tasks.append(str(payload.get("task") or ""))
    if event_callback:
        event_callback(
            "thinking",
            {"message": "本地推理中", "text": "离线思考片段：核对剧情连续性。"},
        )
    if payload.get("task") == "reconcile_story_and_saved_shots":
        story = payload.get("current_story_card") or {}
        return {
            "title": "浏览器同步后的结尾",
            "story_text": str(story.get("story_text") or ""),
            "ending_state": "主体在最终画面中央停稳，动作与故事完整结束。",
            "present_characters": ["甲"],
            "recommended_boundary_before": str(
                payload.get("current_boundary_before") or "continuous"
            ),
            "boundary_reason": "当前边界仍可自然承接。",
            "continuity_compatible": True,
            "conflict_message": "",
            "warnings": [],
        }, {"total_tokens": 13}, "stop"
    return fake_single_stage_provider(messages, settings, event_callback=event_callback)


def main() -> int:
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "Playwright 未安装在当前 Python；请改用已安装 Playwright 的测试环境。"
        ) from exc

    with tempfile.TemporaryDirectory() as temporary:
        runs_root = Path(temporary) / "runs"
        store = longform.LongProjectStore(runs_root)
        store.save(make_story_project("browser_atomic_three", 17))
        server = app.create_server(
            "127.0.0.1",
            0,
            runs_root=runs_root,
            provider_call=browser_provider,
            lmstudio_manager=BrowserLMManager(),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
        requested_paths: list[str] = []
        page_errors: list[str] = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                page.on(
                    "request",
                    lambda request: requested_paths.append(urlsplit(request.url).path),
                )
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.goto(base_url, wait_until="networkidle")
                page.locator("#creativeBrief").fill("离线验证思考流与最终 JSON 分区。")
                page.locator("#generateScriptBtn").click()
                expect(page.locator("#streamStatus")).to_have_text(
                    "剧本完成", timeout=10_000
                )
                expect(page.locator("#streamOutput")).to_contain_text("【思考过程】")
                expect(page.locator("#streamOutput")).to_contain_text(
                    "离线思考片段：核对剧情连续性。"
                )
                expect(page.locator("#streamOutput")).to_contain_text("【最终 JSON】")
                page.locator("#longTabBtn").click()
                expect(page.locator("#longProjectSelect")).to_have_value(
                    "browser_atomic_three", timeout=10_000
                )
                expect(page.locator("#longProjectSummary")).to_be_visible()
                expect(page.locator("#longPromptOutputs details.pending")).to_have_count(3)
                expect(page.locator("#generateAllLongPromptsBtn")).to_be_enabled()

                expect(page.locator("#longSaveStatus")).to_contain_text("已载入工作区")
                selected_segment_id = page.locator(
                    ".long-segment-card.selected"
                ).get_attribute("data-segment-id")
                assert selected_segment_id
                page.locator("#visualStyle").fill("浏览器切换页签保存回归风格")
                page.locator("#shortTabBtn").click()
                expect(page.locator("#longWorkspace")).to_be_hidden(timeout=10_000)
                saved_during_switch = store.load("browser_atomic_three")
                saved_segment = next(
                    item
                    for item in saved_during_switch["segments"]
                    if item["id"] == selected_segment_id
                )
                assert saved_segment["single_workspace"]["form"]["visual_style"] == (
                    "浏览器切换页签保存回归风格"
                ), "切换页签前的单段编辑没有原子保存"
                page.locator("#longTabBtn").click()
                expect(page.locator("#longProjectSelect")).to_have_value(
                    "browser_atomic_three", timeout=10_000
                )

                page.locator("#generateAllLongPromptsBtn").click()
                expect(page.locator("#longTaskTitle")).to_have_text(
                    re.compile(r"(?:completed|failed)"),
                    timeout=15_000,
                )
                task_title = page.locator("#longTaskTitle").inner_text()
                if "failed" in task_title:
                    raise AssertionError(
                        task_title
                        + " | "
                        + page.locator("#longTaskMessage").inner_text()
                        + " | "
                        + page.locator("#longLiveOutput").inner_text()
                    )
                expect(page.locator("#longLiveOutput")).to_contain_text(
                    "离线思考片段：核对剧情连续性。"
                )
                expect(page.locator("#longReadiness")).to_contain_text("3/3")
                expect(page.locator("#longPromptOutputs details.ready")).to_have_count(3)
                expect(page.locator("#startLongRenderBtn")).to_be_disabled()
                expect(page.locator("#confirmAuthoringBtn")).to_be_enabled()
                page.locator("#confirmAuthoringBtn").click()
                expect(page.locator("#startLongRenderBtn")).to_be_enabled()
                expect(page.locator("#lmSessionSummary")).to_contain_text("显存未被本项目占用")
                prompts_before = page.locator(".long-full-prompt").all_text_contents()
                assert len(prompts_before) == 3 and all(prompts_before), prompts_before

                page.get_by_role("button", name="用单段工作台编辑").first.click()
                expect(page.locator("#longSaveStatus")).to_contain_text("v1")
                page.locator("#saveLongWorkspaceBtn").click()
                expect(page.locator("#longSaveStatus")).to_contain_text(
                    "已保存并验证 · 工作区 v2", timeout=10_000
                )
                expect(page.locator("#startLongRenderBtn")).to_be_disabled()
                page.locator(".long-segment-card.selected").get_by_role(
                    "button", name="应用剧情修改"
                ).click()
                expect(page.locator("#toast")).to_contain_text(
                    "剧情内容未变化", timeout=10_000
                )
                expect(page.locator("#longPromptOutputs details.ready")).to_have_count(3)
                expect(page.locator("#startLongRenderBtn")).to_be_disabled()
                page.locator("#confirmAuthoringBtn").click()
                expect(page.locator("#startLongRenderBtn")).to_be_enabled()

                page.reload(wait_until="networkidle")
                page.locator("#longTabBtn").click()
                expect(page.locator("#longProjectSelect")).to_have_value(
                    "browser_atomic_three", timeout=10_000
                )
                expect(page.locator("#longPromptOutputs details.ready")).to_have_count(3)
                expect(page.locator("#longReadiness")).to_contain_text("3/3")
                expect(page.locator("#startLongRenderBtn")).to_be_disabled()
                prompts_after = page.locator(".long-full-prompt").all_text_contents()
                assert prompts_after == prompts_before, "刷新后 H3 提示词没有原样恢复"

                final_card = page.locator(".long-segment-card").nth(2)
                fixed_opening = final_card.locator(
                    '[data-story-field="opening_state"]'
                ).input_value()
                final_card.locator('[data-story-field="story_text"]').fill(
                    "用户在浏览器里修改最后一段中文剧情，主体完成动作并明确收束故事。"
                )
                page.once("dialog", lambda dialog: dialog.accept())
                final_card.get_by_role("button", name="应用剧情修改").click()
                expect(page.locator("#toast")).to_contain_text(
                    "剧情修改已应用", timeout=10_000
                )
                final_card = page.locator(".long-segment-card").nth(2)
                expect(final_card.locator(".segment-state")).to_contain_text("剧情待同步")
                final_card.get_by_role("button", name="同步本段剧情状态").click()
                expect(page.locator("#longTaskTitle")).to_have_text(
                    re.compile(r"剧情状态同步.*completed"), timeout=15_000
                )
                final_card = page.locator(".long-segment-card").nth(2)
                expect(final_card.locator(".segment-state")).to_contain_text("已同步")
                expect(final_card.locator('[data-story-field="opening_state"]')).to_have_value(
                    fixed_opening
                )
                expect(final_card.locator('[data-story-field="ending_state"]')).to_have_value(
                    "主体在最终画面中央停稳，动作与故事完整结束。"
                )
                expect(page.locator("#generateAllLongPromptsBtn")).to_be_enabled()
                page.locator("#generateAllLongPromptsBtn").click()
                expect(page.locator("#longTaskTitle")).to_have_text(
                    re.compile(r"H3 原子预编译.*completed"), timeout=15_000
                )
                expect(page.locator("#longReadiness")).to_contain_text("3/3")
                prompts_after = page.locator(".long-full-prompt").all_text_contents()
                assert len(prompts_after) == 3 and all(prompts_after), prompts_after
                page.locator("#confirmAuthoringBtn").click()
                expect(page.locator("#startLongRenderBtn")).to_be_enabled()

                calls_before_rule_build = len(provider_tasks)
                page.locator("#contextTabBtn").click()
                expect(page.locator("#contextProjectSelect")).to_have_value(
                    "browser_atomic_three", timeout=10_000
                )
                expect(page.locator("#contextGenerateBtn")).to_be_enabled()
                page.locator("#contextGenerateBtn").click()
                expect(page.locator("#contextTaskTitle")).to_have_text(
                    re.compile(r"(?:completed|failed)"), timeout=15_000
                )
                context_task_title = page.locator("#contextTaskTitle").inner_text()
                if "failed" in context_task_title:
                    raise AssertionError(
                        context_task_title
                        + " | "
                        + page.locator("#contextTaskMessage").inner_text()
                        + " | "
                        + page.locator("#contextLiveOutput").inner_text()
                    )
                expect(page.locator("#contextPlanPanel")).to_be_visible()
                expect(page.locator(".context-scene-prompt")).to_have_count(3)
                context_prompts = page.locator(".context-scene-prompt pre").all_text_contents()
                assert len(context_prompts) == 3 and all(context_prompts), context_prompts
                expect(page.locator("#contextDownloadWorkflow")).to_have_attribute(
                    "href", re.compile(r"/context-loop/artifacts/workflow$")
                )
                raw_spec = json.loads(page.locator("#contextRawJson").input_value())
                assert raw_spec["schema_version"] == 2
                assert raw_spec["usage"] == {}
                assert all(
                    scene["prompt"] == expected
                    for scene, expected in zip(raw_spec["scenes"], prompts_after, strict=True)
                )
                assert len(provider_tasks) == calls_before_rule_build
                page.locator(".context-raw-editor > summary").click()
                expect(page.locator("#contextRawJson")).to_have_attribute("readonly", "")
                expect(page.locator("#contextSaveJsonBtn")).to_be_disabled()
                expect(page.locator("#contextRenderBtn")).to_be_disabled()
                assert not page_errors, page_errors
                assert not any(path.endswith("/render") for path in requested_paths)
                assert "/api/comfy/status" not in requested_paths
                browser.close()

            persisted = store.load("browser_atomic_three")
            revisions = [
                item["single_workspace"]["revision"]
                for item in persisted["segments"]
            ]
            assert min(revisions) >= 1 and max(revisions) >= 2, revisions
            assert all(item["h3_prompt"] for item in persisted["segments"])
            print(
                "BROWSER_E2E_OK: 3/3 prompts persisted; rule workflow copied them "
                "with zero provider calls; render stayed idle"
            )
            return 0
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
