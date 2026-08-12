"""Isolated runtime for the optional unattended H3 Context Loop mode.

The stable long-project schema and its existing render scheduler do not depend
on this module.  Context Loop plans are sidecars and use their own endpoints,
tasks, validation, and ComfyUI workflow compiler.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Callable

from context_loop import (
    ContextLoopStore,
    assess_staleness,
    build_rule_spec,
    generation_fingerprint,
)
from longform import LongFormError, LongProjectStore, utc_now
from longform_runtime import (
    BackgroundTask,
    ComfyClient,
    _comfy_combo_choices,
    _project_artifact_receipt,
    _provider_error,
    assemble_master_video,
    ensure_media_backend,
    inspect_media,
)
from project_assets import ProjectAssetStore
from tools.build_context_workflow import (
    build_api_workflow,
    build_ui_workflow,
    build_upscale_api_workflow,
    plugin_plan,
    validate_api_workflow,
    validate_upscale_api_workflow,
)


ProviderCall = Callable[..., tuple[dict[str, Any], dict[str, Any], str | None]]
CONTEXT_PLUGIN_COMMIT = "81e615c66384e8f747ded5d181ef5807f2775daa"
CONTEXT_PLUGIN_VERSION = "0.3.20"
CONTEXT_ARTIFACTS = {
    "plan": "plan.json",
    "api_prompt": "api_prompt.json",
    "workflow": "workflow.json",
    "spec": "spec.json",
}


def _reference_fingerprint(
    spec: dict[str, Any], assets: ProjectAssetStore
) -> str:
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for scene in spec.get("scenes") or []:
        for slot in ("first_frame", "last_frame"):
            item = scene.get(slot) if isinstance(scene, dict) else None
            if not isinstance(item, dict) or item.get("source") == "previous_tail":
                continue
            if item.get("source") != "project_asset":
                raise LongFormError(
                    "规则工作流仍引用旧版 ComfyUI input 图片；请重新选择并保存项目图片。",
                    "context_reference_migration_required",
                )
            asset_id = str(item.get("asset_id") or "")
            if asset_id in seen:
                continue
            seen.add(asset_id)
            path = assets.resolve(str(spec["project_id"]), item)
            items.append(
                {
                    "role": "project_keyframe",
                    "name": path.name,
                    "sha256": asset_id,
                }
            )
    payload = {
        "logical": generation_fingerprint(spec),
        "files": items,
        "plugin_commit": CONTEXT_PLUGIN_COMMIT,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ContextComfyClient(ComfyClient):
    REQUIRED_CONTEXT_NODES = {
        "H3PromptStudioRulePlan",
        "H3PromptStudioKeyframeRouter",
        "H3PromptStudioExactFrameMap",
        "MiniMaxH3ChainLoopStart",
        "MiniMaxH3ChainCurrent",
        "MiniMaxH3ChainSegmentSave",
        "MiniMaxH3ChainLoopEnd",
        "MiniMaxH3ChainAssemble",
        "MiniMaxH3HybridRefAndKeyframe",
        "MiniMaxH3TurboLoRA",
        "MiniMaxH3TurboSampler",
        "H3Idea2VideoProjectFileCopy",
    }
    REQUIRED_UPSCALE_NODES = {
        "LoadVideo",
        "GetVideoComponents",
        "LoadAudio",
        "UpscaleModelLoader",
        "ImageUpscaleWithModel",
        "ImageScale",
        "CreateVideo",
        "H3Idea2VideoProjectVideoSave",
    }

    def preflight_context(self, *, upscale: bool = False) -> dict[str, Any]:
        info = self._request("/object_info")
        if not isinstance(info, dict):
            raise LongFormError("ComfyUI object_info 无效。", "comfy_response_invalid")
        self.preflight(info=info, require_upscale=upscale)
        required = set(self.REQUIRED_CONTEXT_NODES)
        if upscale:
            required.update(self.REQUIRED_UPSCALE_NODES)
        missing = sorted(required - set(info))
        if missing:
            raise LongFormError(
                "ComfyUI 尚未加载 Context Loop 节点："
                + ", ".join(missing)
                + "。请先运行 install_comfyui_nodes.bat，再手动重启 ComfyUI。",
                "context_nodes_missing",
            )
        if upscale:
            try:
                descriptor = info["UpscaleModelLoader"]["input"]["required"]["model_name"]
            except (KeyError, IndexError, TypeError):
                descriptor = None
            choices = _comfy_combo_choices(descriptor)
            if choices is None or "RealESRGAN_x2plus.pth" not in choices:
                raise LongFormError(
                    "ComfyUI 未找到 RealESRGAN_x2plus.pth；请安装模型或关闭 1080p 后处理。",
                    "context_upscale_model_missing",
                )
        return {
            "ok": True,
            "missing_nodes": [],
            "plugin_version": CONTEXT_PLUGIN_VERSION,
            "plugin_commit": CONTEXT_PLUGIN_COMMIT,
            "client_id": self.client_id,
        }

    def submit_context(self, workflow: dict[str, Any]) -> str:
        errors = validate_api_workflow(workflow)
        if errors:
            raise LongFormError(
                "Context Loop API 工作流无效：" + "; ".join(errors),
                "context_workflow_invalid",
            )
        response = self._request(
            "/prompt",
            {
                "prompt": workflow["prompt"],
                "client_id": self.client_id,
                "extra_data": {"h3_prompt_studio_context": workflow.get("meta") or {}},
            },
        )
        if not isinstance(response, dict) or not response.get("prompt_id"):
            error = response.get("error") if isinstance(response, dict) else None
            raise LongFormError(
                "ComfyUI 拒绝了 Context Loop 工作流。" + (f" {error}" if error else ""),
                "comfy_prompt_rejected",
            )
        return str(response["prompt_id"])

    def submit_upscale(self, workflow: dict[str, Any]) -> str:
        errors = validate_upscale_api_workflow(workflow)
        if errors:
            raise LongFormError(
                "1080p 后处理 API 工作流无效：" + "; ".join(errors),
                "context_upscale_workflow_invalid",
            )
        response = self._request(
            "/prompt",
            {
                "prompt": workflow["prompt"],
                "client_id": self.client_id,
                "extra_data": {"h3_prompt_studio_context_upscale": workflow.get("meta") or {}},
            },
        )
        if not isinstance(response, dict) or not response.get("prompt_id"):
            raise LongFormError("ComfyUI 拒绝了 1080p 后处理工作流。", "comfy_prompt_rejected")
        return str(response["prompt_id"])

    def checkpoints(self, run_name: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"run_name": str(run_name or "")})
        value = self._request("/minimax_h3_context_loop/checkpoints?" + query)
        if not isinstance(value, dict) or not isinstance(value.get("checkpoints"), list):
            raise LongFormError(
                "Context Loop checkpoint API 响应无效。", "comfy_response_invalid"
            )
        return [item for item in value["checkpoints"] if isinstance(item, dict)]


class ContextLoopRuntime:
    def __init__(
        self,
        *,
        store: LongProjectStore,
        project_root: Path,
        provider_call: ProviderCall,
        comfy_client_factory: Callable[[], Any] = ContextComfyClient,
        render_lock: threading.Lock | None = None,
        media_inspector: Callable[..., dict[str, Any]] = inspect_media,
        master_assembler: Callable[..., dict[str, Any]] = assemble_master_video,
    ) -> None:
        self.store = store
        self.sidecars = ContextLoopStore(store)
        self.project_root = Path(project_root)
        self.assets = ProjectAssetStore(store)
        # Kept in the constructor for create_server/test compatibility. The
        # rule compiler deliberately has no provider path.
        del provider_call
        self.comfy_client_factory = comfy_client_factory
        self.media_inspector = media_inspector
        self.master_assembler = master_assembler
        self._requires_media_backend = (
            media_inspector is inspect_media or master_assembler is assemble_master_video
        )
        self._render_lock = render_lock or threading.Lock()
        self._generation_lock = threading.Lock()
        self._tasks: dict[str, BackgroundTask] = {}
        self._lock = threading.RLock()

    def task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise LongFormError("Context Loop 后台任务不存在。", "task_not_found")
            return task.public()

    def tasks_for_project(self, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                task.public()
                for task in self._tasks.values()
                if task.project_id == project_id
            ][-10:]

    def _new_task(self, kind: str, project_id: str) -> BackgroundTask:
        task = BackgroundTask(
            id="context_" + kind + "_" + uuid.uuid4().hex[:12],
            kind="context_" + kind,
            project_id=project_id,
        )
        with self._lock:
            self._tasks[task.id] = task
        return task

    @staticmethod
    def _set_task(task: BackgroundTask, **values: Any) -> None:
        for name, value in values.items():
            setattr(task, name, value)
        task.updated_at = utc_now()

    def public(self, project_id: str) -> dict[str, Any]:
        project = self.store.load(project_id)
        return {
            "context_loop": self.sidecars.public(project),
            "tasks": self.tasks_for_project(project_id),
            "plugin": {
                "version": CONTEXT_PLUGIN_VERSION,
                "commit": CONTEXT_PLUGIN_COMMIT,
                "installed": self.plugin_status()["installed"],
            },
        }

    def plugin_status(self) -> dict[str, Any]:
        source = self.project_root / "vendor" / "minimax-h3-contex-loop"
        adapter_source = self.project_root / "comfyui_nodes" / "H3PromptStudioRuleAdapter"
        installed = False
        connection_error = ""
        try:
            info = self.comfy_client_factory()._request("/object_info")
            installed = isinstance(info, dict) and ContextComfyClient.REQUIRED_CONTEXT_NODES.issubset(info)
        except Exception as exc:
            connection_error = str(exc)
        return {
            "installed": installed,
            "connection_error": connection_error,
            "source": str(source),
            "adapter_source": str(adapter_source),
            "version": CONTEXT_PLUGIN_VERSION,
            "commit": CONTEXT_PLUGIN_COMMIT,
        }

    def start_generation(
        self,
        project_id: str,
        payload: dict[str, Any],
        settings: Any = None,
    ) -> dict[str, Any]:
        """Build a read-only execution plan from already accepted project data."""

        project = self.store.load(project_id)
        existing = self.sidecars.load_optional(project_id)
        base_seed_value = payload.get("base_seed")
        if base_seed_value in {None, ""}:
            base_seed = (
                int(str(existing.get("base_seed")))
                if existing and existing.get("base_seed") not in {None, ""}
                else None
            )
        else:
            try:
                base_seed = int(base_seed_value)
            except (TypeError, ValueError) as exc:
                raise LongFormError("基础 Seed 必须是整数。", "context_seed_invalid") from exc
        upscale_value = payload.get("upscale_1080")
        task = self._new_task("build", project_id)

        def worker() -> None:
            with self._generation_lock:
                try:
                    self._set_task(
                        task,
                        state="running",
                        stage="rule_compile",
                        current=0,
                        total=len(project.get("segments") or []),
                        message="按规则读取已保存的 H3 提示词与图片路由…",
                        live_text="",
                    )
                    latest = self.store.load(project_id)
                    spec = build_rule_spec(
                        latest,
                        existing=existing,
                        base_seed=base_seed,
                        upscale_1080=(
                            bool(upscale_value) if upscale_value is not None else None
                        ),
                    )
                    start_scene = int(payload.get("start_scene") or 1)
                    if start_scene < 1 or start_scene > len(spec["scenes"]):
                        raise LongFormError(
                            "恢复起始场景无效。", "context_scene_index_invalid"
                        )
                    spec["render"]["start_scene"] = start_scene
                    saved = self.sidecars.save(
                        spec,
                        expected_revision=int((existing or {}).get("revision") or 0),
                        reason="deterministic rule compile from project prompts",
                    )
                    self._write_artifacts(saved)
                    task.result = {
                        "compiled": True,
                        "scene_count": len(saved["scenes"]),
                        "source": "segments[].h3_prompt",
                        "provider_calls": 0,
                    }
                    self._set_task(
                        task,
                        state="completed",
                        stage="done",
                        current=len(saved["scenes"]),
                        total=len(saved["scenes"]),
                        message="规则工作流已生成：提示词逐字复用项目内容，未调用 Qwen，尚未提交 GPU。",
                    )
                except Exception as exc:
                    message, code = _provider_error(exc)
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
            name=f"h3-context-rule-build-{project_id}",
            daemon=True,
        ).start()
        return {"task": task.public(), "project_id": project_id}

    def save_spec(
        self,
        project_id: str,
        raw: Any,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        del project_id, raw, expected_revision
        raise LongFormError(
            "规则执行计划是只读派生物；请回到长剧本项目修改剧情、Shot 或 H3 提示词后重新生成工作流。",
            "context_read_only",
        )

    def _write_artifacts(self, spec: dict[str, Any]) -> None:
        root = self.sidecars.root(str(spec["project_id"]))
        api = build_api_workflow(spec)
        documents = {
            "plan": plugin_plan(spec),
            "api_prompt": api,
            "workflow": build_ui_workflow(api),
        }
        for name, document in documents.items():
            self.store._atomic_json(root / CONTEXT_ARTIFACTS[name], document)

    def artifact_path(self, project_id: str, kind: str) -> Path:
        if kind not in CONTEXT_ARTIFACTS:
            raise LongFormError("未知的规则工作流文件。", "context_artifact_invalid")
        path = self.sidecars.root(project_id) / CONTEXT_ARTIFACTS[kind]
        if not path.is_file():
            raise LongFormError("规则工作流文件尚未生成。", "context_artifact_not_found")
        return path

    def _plugin_scene_artifacts(
        self, spec: dict[str, Any], client: Any
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for index, scene in enumerate(spec.get("scenes") or [], start=1):
            try:
                raw = client.view_bytes(
                    name=(
                        f"h3_chains/{spec['run_name']}/checkpoints/"
                        f"clip_{index:04d}.json"
                    ),
                    file_type="output",
                )
                metadata = json.loads(raw.decode("utf-8"))
            except (LongFormError, UnicodeError, json.JSONDecodeError) as exc:
                raise LongFormError(
                    f"缺少第 {index} 段 Context Loop checkpoint 元数据。",
                    "context_checkpoint_missing",
                ) from exc
            segment = metadata.get("segment") if isinstance(metadata, dict) else None
            if not isinstance(segment, dict):
                raise LongFormError("Context Loop checkpoint 元数据无效。", "context_checkpoint_invalid")
            if int(segment.get("index") or 0) != index or str(segment.get("id") or "") != str(scene["id"]):
                raise LongFormError(
                    f"第 {index} 段 checkpoint 与当前计划不匹配。",
                    "context_checkpoint_invalid",
                )
            video_name = str(segment.get("segment") or "")
            audio_name = str(segment.get("generated_audio") or "")
            if not video_name or not audio_name:
                raise LongFormError(
                    f"第 {index} 段缺少原生视频或生成音频 sidecar。",
                    "context_checkpoint_invalid",
                )
            video_hash = str(segment.get("segment_sha256") or "")
            audio_hash = str(segment.get("generated_audio_sha256") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", video_hash) or not re.fullmatch(
                r"[0-9a-f]{64}", audio_hash
            ):
                raise LongFormError(
                    f"第 {index} 段 checkpoint 缺少产物哈希。",
                    "context_checkpoint_invalid",
                )
            result.append(
                {
                    "index": index,
                    "id": str(scene["id"]),
                    "frames": int(scene["delivered_frames"]),
                    "video_name": video_name.replace("\\", "/") + " [output]",
                    "audio_name": audio_name.replace("\\", "/") + " [output]",
                    "source_sha256": hashlib.sha256(
                        (video_hash + ":" + audio_hash).encode("ascii")
                    ).hexdigest(),
                }
            )
        return result

    def _resume_scene_hint(
        self, spec: dict[str, Any], fallback: int, client: Any | None = None
    ) -> int:
        count = len(spec.get("scenes") or [])
        try:
            active_client = client or self.comfy_client_factory()
            checkpoints = active_client.checkpoints(str(spec.get("run_name") or ""))
            ready = {
                int(item.get("scene") or 0)
                for item in checkpoints
                if item.get("ready") is True
            }
            completed = 0
            for index in range(1, count + 1):
                if index not in ready:
                    break
                completed = index
        except Exception:
            completed = 0
        if 0 < completed < count:
            return completed + 1
        if completed >= count and count:
            return count
        return max(1, min(count or 1, int(fallback)))

    def _upscale_all_scenes(
        self,
        *,
        spec: dict[str, Any],
        client: Any,
        task: BackgroundTask,
    ) -> dict[str, Any]:
        artifacts = self._plugin_scene_artifacts(spec, client)
        render = spec.setdefault("render", {})
        previous = {
            int(item.get("index") or 0): item
            for item in render.get("upscale_segments") or []
            if isinstance(item, dict)
        }
        accepted: list[dict[str, Any]] = []
        output_dir = self.sidecars.root(str(spec["project_id"])) / "output" / "upscale_segments"
        output_dir.mkdir(parents=True, exist_ok=True)
        for artifact in artifacts:
            index = int(artifact["index"])
            cached = previous.get(index)
            cached_path = Path(str((cached or {}).get("path") or ""))
            if (
                cached
                and str(cached.get("source_sha256") or "") == artifact["source_sha256"]
                and cached_path.is_file()
            ):
                accepted.append(copy.deepcopy(cached))
                continue
            self._set_task(
                task,
                stage="upscale",
                current=index,
                total=len(artifacts),
                message=f"RealESRGAN 正在处理第 {index}/{len(artifacts)} 段；H3 采样已完成。",
            )
            workflow = build_upscale_api_workflow(
                source_video=str(artifact["video_name"]),
                source_audio=str(artifact["audio_name"]),
                project_id=str(spec["project_id"]),
                relative_path=(
                    f"{spec['project_id']}/context_loop/output/"
                    f"upscale_segments/clip_{index:04d}.mp4"
                ),
            )
            client.wait_until_idle(stop=lambda: task.stop_requested)
            if task.stop_requested:
                raise LongFormError("已在下一个 1080p 分段提交前暂停。", "task_paused")
            prompt_id = client.submit_upscale(workflow)
            history = client.wait_history(prompt_id, stop=lambda: task.stop_requested)
            receipt, paths = _project_artifact_receipt(
                history,
                workflow["meta"]["output_nodes"]["video"],
                project_root=self.store.root.parent,
            )
            destination = paths[-1]
            media = self.media_inspector(
                destination,
                expected_frames=int(artifact["frames"]),
                expected_size=(1080, 1920),
                require_audio=True,
            )
            entry = {
                "index": index,
                "scene_id": artifact["id"],
                "source_sha256": artifact["source_sha256"],
                "path": str(destination),
                "prompt_id": prompt_id,
                "receipt": receipt,
                "media": media,
                "completed_at": utc_now(),
            }
            accepted.append(entry)
            render["upscale_segments"] = sorted(accepted, key=lambda item: int(item["index"]))
            spec = self.sidecars.save(
                spec,
                expected_revision=int(spec["revision"]),
                reason=f"upscaled context scene {index}",
            )
            render = spec.setdefault("render", {})
        paths = [Path(item["path"]) for item in sorted(accepted, key=lambda item: int(item["index"]))]
        if len(paths) != len(artifacts):
            raise LongFormError("1080p 分段集合不完整。", "context_upscale_incomplete")
        master = self.sidecars.root(str(spec["project_id"])) / "output" / "master_1080x1920.mp4"
        master_info = self.master_assembler(
            paths,
            master,
            expected_frames=int(spec["total_delivered_frames"]),
        )
        spec["render"]["upscaled_path"] = str(master)
        spec["render"]["upscale_master"] = master_info
        spec["render"]["upscale_segments"] = sorted(accepted, key=lambda item: int(item["index"]))
        return spec

    def start_render(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        project = self.store.load(project_id)
        spec = assess_staleness(self.sidecars.load(project_id), project)
        if spec.get("status") != "valid" or spec.get("stale"):
            raise LongFormError("规则工作流无效或已过期，请从当前长项目重新生成。", "context_plan_not_ready")
        start_scene = int(
            payload.get("start_scene")
            or (spec.get("render") or {}).get("start_scene")
            or 1
        )
        if start_scene < 1 or start_scene > len(spec.get("scenes") or []):
            raise LongFormError("GPU 起始场景无效。", "context_scene_index_invalid")
        if self._requires_media_backend:
            ensure_media_backend()
        task = self._new_task("render", project_id)

        def worker() -> None:
            with self._render_lock:
                saved: dict[str, Any] | None = None
                try:
                    current_project = self.store.load(project_id)
                    saved = assess_staleness(self.sidecars.load(project_id), current_project)
                    if saved.get("stale") or saved.get("status") != "valid":
                        raise LongFormError("提交前检测到规则工作流已过期。", "context_plan_not_ready")
                    fingerprint = _reference_fingerprint(saved, self.assets)
                    workflow = build_api_workflow(
                        saved,
                        start_scene=start_scene,
                        generation_fingerprint_override=fingerprint,
                    )
                    client = self.comfy_client_factory()
                    self._set_task(task, state="running", stage="preflight", message="检查 Context Loop 节点和输入图片。")
                    if hasattr(client, "preflight_context"):
                        client.preflight_context(
                            upscale=bool((saved.get("outputs") or {}).get("upscale_1080"))
                        )
                    else:
                        raise LongFormError("Comfy 客户端不支持 Context Loop 预检。", "context_client_invalid")
                    self._set_task(task, stage="waiting_comfy", message="等待 ComfyUI 队列空闲；不会清空或打断现有任务。")
                    client.wait_until_idle(stop=lambda: task.stop_requested)
                    if task.stop_requested:
                        raise LongFormError("已在 GPU 提交前暂停。", "task_paused")
                    self._set_task(
                        task,
                        stage="gpu",
                        current=start_scene,
                        total=len(saved["scenes"]),
                        message=f"ComfyUI 正在从第 {start_scene} 段连续生成到结尾。",
                    )
                    prompt_id = client.submit_context(workflow)
                    saved["render"] = {
                        **(saved.get("render") or {}),
                        "state": "running",
                        "prompt_id": prompt_id,
                        "start_scene": start_scene,
                        "last_error": None,
                        "submitted_at": utc_now(),
                    }
                    saved = self.sidecars.save(
                        saved,
                        expected_revision=int(saved["revision"]),
                        reason=f"submitted context render {prompt_id}",
                    )
                    history = client.wait_history(prompt_id, stop=lambda: task.stop_requested)
                    native_receipt, native_paths = _project_artifact_receipt(
                        history,
                        workflow["meta"]["output_nodes"]["project_native"],
                        project_root=self.store.root.parent,
                    )
                    destination = native_paths[-1]
                    native_media = self.media_inspector(
                        destination,
                        expected_frames=int(saved["total_delivered_frames"]),
                        expected_size=(768, 1344),
                        require_audio=True,
                    )
                    wants_upscale = bool((saved.get("outputs") or {}).get("upscale_1080"))
                    saved["render"] = {
                        **(saved.get("render") or {}),
                        "state": "upscaling" if wants_upscale else "completed",
                        "prompt_id": prompt_id,
                        "start_scene": 1,
                        "native_path": str(destination),
                        "native_media": native_media,
                        "native_receipt": native_receipt,
                        "upscaled_path": str((saved.get("render") or {}).get("upscaled_path") or ""),
                        "native_completed_at": utc_now(),
                        "last_error": None,
                    }
                    saved = self.sidecars.save(
                        saved,
                        expected_revision=int(saved["revision"]),
                        reason="context native render completed",
                    )
                    if wants_upscale:
                        saved = self._upscale_all_scenes(
                            spec=saved,
                            client=client,
                            task=task,
                        )
                    saved["render"]["state"] = "completed"
                    saved["render"]["start_scene"] = 1
                    saved["render"]["completed_at"] = utc_now()
                    saved["render"]["last_error"] = None
                    saved = self.sidecars.save(
                        saved,
                        expected_revision=int(saved["revision"]),
                        reason="context render completed",
                    )
                    self._set_task(
                        task,
                        state="completed",
                        stage="done",
                        current=len(saved["scenes"]),
                        total=len(saved["scenes"]),
                        message=(
                            f"原生总片与 1080p 总片均已保存。"
                            if wants_upscale
                            else f"原生总片已保存：{destination.name}"
                        ),
                    )
                except Exception as exc:
                    message, code = _provider_error(exc)
                    if saved is not None:
                        try:
                            failed_spec = self.sidecars.load(project_id)
                            failed_spec["render"] = {
                                **(failed_spec.get("render") or {}),
                                "state": "paused" if code == "task_paused" else "failed",
                                "start_scene": self._resume_scene_hint(
                                    failed_spec, start_scene, locals().get("client")
                                ),
                                "last_error": {"code": code, "message": message},
                            }
                            self.sidecars.save(
                                failed_spec,
                                expected_revision=int(failed_spec.get("revision") or 0),
                                reason="context render stopped",
                            )
                        except Exception:
                            pass
                    self._set_task(
                        task,
                        state="paused" if code == "task_paused" else "failed",
                        stage="failed",
                        message=message,
                        error={"code": code, "message": message},
                    )
                finally:
                    task.finished_at = utc_now()
                    task.updated_at = task.finished_at

        threading.Thread(
            target=worker,
            name=f"h3-context-render-{project_id}",
            daemon=True,
        ).start()
        return {"task": task.public(), "project_id": project_id}

    def request_stop(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise LongFormError("Context Loop 后台任务不存在。", "task_not_found")
            task.stop_requested = True
            task.updated_at = utc_now()
            if task.state == "queued":
                task.message = "已请求在提交前暂停。"
            return task.public()
