"""Thin deterministic adapters for H3 Prompt Studio's rule workflow.

No provider is imported or called here. The nodes validate a server-built plan,
route saved keyframes, and map native H3 output onto the project's exact 24-fps
frame allocation. MiniMax sampling and checkpoint persistence stay in the
official/local H3 and vendored Context Loop components.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import torch
import torch.nn.functional as torch_functional


PLAN_TYPE = "H3_CHAIN_PLAN"
STATE_TYPE = "H3_CHAIN_STATE"
MAX_PLAN_BYTES = 4_000_000
MAX_SHOTS = 128
MAX_SEED = 0xFFFFFFFFFFFFFFFF
PROJECT_MARKER = ".h3-idea2video-root"
PROJECT_MARKER_CONTENT = "MiniMax-H3-Idea2Video-tool"
MAX_RELATIVE_PATH_BYTES = 4096


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_project_root(project_root: str) -> Path:
    root_text = str(project_root or "").strip()
    if root_text == "$IDEA2VIDEO_PROJECT_ROOT":
        root_text = str(Path(__file__).resolve().parents[2])
    if not root_text:
        raise ValueError("Idea2Video project root is required.")
    root = Path(root_text).expanduser().resolve(strict=True)
    marker = root / PROJECT_MARKER
    try:
        marker_content = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError(
            "Idea2Video project marker is missing; refusing external file access."
        ) from exc
    if marker_content != PROJECT_MARKER_CONTENT:
        raise ValueError("Idea2Video project marker is missing; refusing external file access.")
    return root


def _project_destination(project_root: str, relative_path: str) -> tuple[Path, Path]:
    """Resolve one write below a marker-gated project ``runs`` directory."""

    relative_text = str(relative_path or "").strip().replace("\\", "/")
    if not relative_text:
        raise ValueError("Idea2Video relative output path is required.")
    if len(relative_text.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES:
        raise ValueError("Idea2Video relative output path is too long.")
    relative = PurePosixPath(relative_text)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Idea2Video output path must be a safe relative runs path.")
    root = _resolve_project_root(project_root)
    runs = root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    runs_resolved = runs.resolve(strict=True)
    candidate = runs.joinpath(*relative.parts).resolve(strict=False)
    try:
        candidate.relative_to(runs_resolved)
    except ValueError as exc:
        raise ValueError("Idea2Video output path escapes the project runs directory.") from exc
    candidate.parent.mkdir(parents=True, exist_ok=True)
    # Resolve again after creating parents so an existing junction/reparse point
    # cannot redirect the write outside the marker-gated runs directory.
    resolved_parent = candidate.parent.resolve(strict=True)
    try:
        resolved_parent.relative_to(runs_resolved)
    except ValueError as exc:
        raise ValueError("Idea2Video output parent escapes through a link.") from exc
    return root, resolved_parent / candidate.name


def _receipt(kind: str, project_root: Path, paths: list[Path]) -> str:
    value = {
        "schema": "h3_idea2video_artifact_receipt_v1",
        "kind": kind,
        "files": [
            {
                "relative_path": path.resolve().relative_to(project_root.resolve()).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
            for path in paths
        ],
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _node_result(receipt: str):
    return {"ui": {"text": [receipt]}, "result": (receipt,)}


def _uniform_indices(source_frames: int, output_frames: int, skip_first: bool) -> list[int]:
    start = 1 if skip_first else 0
    end = int(source_frames) - 1
    count = int(output_frames)
    if count < 1 or end - start + 1 < count:
        raise ValueError(
            "H3 Prompt Studio cannot map %d native frames to %d delivered frames."
            % (source_frames, output_frames)
        )
    if count == 1:
        return [end]
    span = end - start
    denominator = count - 1
    values = [
        start + (index * span + denominator // 2) // denominator
        for index in range(count)
    ]
    if values[0] != start or values[-1] != end or len(set(values)) != count:
        raise ValueError("H3 Prompt Studio exact frame mapping lost an endpoint.")
    return values


def _resize_frame(image: Any, width: int, height: int) -> Any:
    if image is None:
        return None
    if not torch.is_tensor(image) or image.ndim != 4 or int(image.shape[0]) < 1:
        raise ValueError("H3 Prompt Studio keyframe must be an IMAGE batch.")
    frame = image[:1]
    if int(frame.shape[1]) == int(height) and int(frame.shape[2]) == int(width):
        return frame
    channels_first = frame.movedim(-1, 1)
    resized = torch_functional.interpolate(
        channels_first,
        size=(int(height), int(width)),
        mode="bicubic",
        align_corners=False,
        antialias=True,
    )
    return resized.movedim(1, -1).clamp(0.0, 1.0)


def _remap_audio(
    waveform: Any,
    *,
    sample_rate: int,
    raw_frames: int,
    target_frames: int,
    skip_first: bool,
) -> Any:
    """Map the complete native audio span onto the exact delivered duration."""

    if not torch.is_tensor(waveform) or waveform.ndim not in {1, 2, 3}:
        raise ValueError("H3 Prompt Studio decoded audio waveform is invalid.")
    original_ndim = waveform.ndim
    if original_ndim == 1:
        prepared = waveform.reshape(1, 1, -1)
    elif original_ndim == 2:
        prepared = waveform.unsqueeze(0)
    else:
        prepared = waveform
    native_samples = int(round(raw_frames / 24.0 * sample_rate))
    start_sample = int(round((1.0 / 24.0 if skip_first else 0.0) * sample_rate))
    wanted = int(round(target_frames / 24.0 * sample_rate))
    if int(prepared.shape[-1]) < native_samples:
        prepared = torch_functional.pad(
            prepared,
            (0, native_samples - int(prepared.shape[-1])),
        )
    native = prepared[..., start_sample:native_samples]
    if int(native.shape[-1]) < 2:
        raise ValueError("H3 Prompt Studio decoded audio is too short to remap.")
    if int(native.shape[-1]) != wanted:
        source_dtype = native.dtype
        native = torch_functional.interpolate(
            native.to(dtype=torch.float32),
            size=wanted,
            mode="linear",
            align_corners=True,
        ).to(dtype=source_dtype)
    result = native.contiguous()
    if original_ndim == 1:
        return result.reshape(-1)
    if original_ndim == 2:
        return result.squeeze(0)
    return result


def _load_input_image(name: str, width: int, height: int) -> Any:
    if not str(name or "").strip():
        raise ValueError("H3 Prompt Studio saved keyframe name is empty.")
    import nodes as comfy_nodes

    image, _mask = comfy_nodes.LoadImage().load_image(str(name))
    return _resize_frame(image, width, height)


def _load_project_asset(plan: dict[str, Any], spec: dict[str, Any], width: int, height: int) -> Any:
    project_id = str(plan.get("project_id") or "")
    asset_id = str(spec.get("asset_id") or "")
    relative_text = str(spec.get("relative_path") or "").replace("\\", "/")
    if not project_id or Path(project_id).name != project_id:
        raise ValueError("Idea2Video plan project id is invalid.")
    if len(asset_id) != 64 or any(ch not in "0123456789abcdef" for ch in asset_id):
        raise ValueError("Idea2Video project asset id is invalid.")
    relative = PurePosixPath(relative_text)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.parts[0] != "assets"
        or any(part in {"", ".", ".."} for part in relative.parts)
        or not relative.name.startswith(asset_id + ".")
    ):
        raise ValueError("Idea2Video project asset path is invalid.")
    root = _resolve_project_root(str(plan.get("project_root") or ""))
    assets_root = (root / "runs" / project_id / "assets").resolve(strict=True)
    path = (root / "runs" / project_id).joinpath(*relative.parts).resolve(strict=True)
    try:
        path.relative_to(assets_root)
    except ValueError as exc:
        raise ValueError("Idea2Video project asset escapes its asset directory.") from exc
    if not path.is_file() or _sha256_file(path) != asset_id:
        raise ValueError("Idea2Video project asset is missing or failed SHA-256.")
    from PIL import Image
    import numpy as np

    with Image.open(path) as image:
        rgb = image.convert("RGB")
        array = np.asarray(rgb).astype(np.float32) / 255.0
    tensor = torch.from_numpy(array).unsqueeze(0)
    return _resize_frame(tensor, width, height)


def _validate_plan(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or int(raw.get("version", -1)) != 2:
        raise ValueError("H3 Prompt Studio rule plan version must be 2.")
    shots = raw.get("shots")
    if not isinstance(shots, list) or not shots or len(shots) > MAX_SHOTS:
        raise ValueError("H3 Prompt Studio rule plan requires 1..128 shots.")
    compatibility = raw.get("compatibility")
    if not isinstance(compatibility, dict):
        raise ValueError("H3 Prompt Studio rule plan has no compatibility contract.")
    expected = {
        "fps": 24,
        "width": 768,
        "height": 1344,
        "context_length": 1,
        "encode_mode": "frames",
        "anchor_mode": "before",
        "crop": "disabled",
        "audio_mode": "generated_audio",
        "audio_context_length": 1,
        "segment_crf": 18,
    }
    for key, value in expected.items():
        if compatibility.get(key) != value:
            raise ValueError(
                "H3 Prompt Studio rule plan compatibility %s must be %r."
                % (key, value)
            )
    delivered_before = 0
    normalized_shots: list[dict[str, Any]] = []
    uses_project_assets = False
    for index, incoming in enumerate(shots, start=1):
        if not isinstance(incoming, dict):
            raise ValueError("H3 Prompt Studio shot %d is not an object." % index)
        shot = copy.deepcopy(incoming)
        if int(shot.get("index", -1)) != index:
            raise ValueError("H3 Prompt Studio shot indexes must be contiguous.")
        prompt = shot.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("H3 Prompt Studio shot %d has no complete prompt." % index)
        if shot.get("scene_prompt") != prompt:
            raise ValueError("H3 Prompt Studio never permits a second transformed prompt.")
        expected_prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if shot.get("prompt_hash") != expected_prompt_hash:
            raise ValueError("H3 Prompt Studio shot %d prompt hash mismatch." % index)
        raw_frames = int(shot.get("raw_frames", 0))
        target_frames = int(shot.get("target_frames", 0))
        if raw_frames < 5 or raw_frames % 17 != 5:
            raise ValueError("H3 Prompt Studio shot %d raw frames are not 17k+5." % index)
        if target_frames < 1 or int(shot.get("delivered_frames", -1)) != target_frames:
            raise ValueError("H3 Prompt Studio shot %d target frame contract is invalid." % index)
        if int(shot.get("generation_start_frame", -1)) != delivered_before:
            raise ValueError("H3 Prompt Studio shot %d timeline offset is invalid." % index)
        if int(shot.get("steps", 0)) != 8:
            raise ValueError("H3 Prompt Studio rule workflow is locked to 8 steps.")
        seed = int(shot.get("seed", -1))
        if seed < 0 or seed > MAX_SEED:
            raise ValueError("H3 Prompt Studio shot seed is outside uint64.")
        mode = str(shot.get("mode") or "")
        boundary = str(shot.get("boundary_before") or "")
        first = shot.get("first_frame")
        last = shot.get("last_frame")
        if mode not in {"T2VA", "I2VA", "FL2VA"}:
            raise ValueError("H3 Prompt Studio shot %d has an invalid mode." % index)
        if index == 1 and boundary != "start":
            raise ValueError("H3 Prompt Studio first shot boundary must be start.")
        if index > 1 and boundary not in {"continuous", "cut"}:
            raise ValueError("H3 Prompt Studio shot boundary is invalid.")
        if boundary == "continuous":
            if not isinstance(first, dict) or first.get("source") != "previous_tail":
                raise ValueError("Continuous shots must use the previous delivered tail.")
            if raw_frames <= target_frames:
                raise ValueError("Continuous shots must reserve a native conditioning frame.")
        elif mode in {"I2VA", "FL2VA"}:
            if not isinstance(first, dict) or first.get("source") not in {"input", "project_asset"}:
                raise ValueError("Independent I2VA/FL2VA shots require a saved Picture 1.")
        elif first is not None:
            raise ValueError("T2VA shots must not route Picture 1.")
        if mode == "FL2VA":
            if not isinstance(last, dict) or last.get("source") not in {"input", "project_asset"}:
                raise ValueError("FL2VA shots require a saved Picture 2.")
        elif last is not None:
            raise ValueError("Only FL2VA shots may route Picture 2.")
        uses_project_assets = uses_project_assets or any(
            isinstance(item, dict) and item.get("source") == "project_asset"
            for item in (first, last)
        )
        delivered_before += target_frames
        normalized_shots.append(shot)
    if int(raw.get("total_delivered_frames", -1)) != delivered_before:
        raise ValueError("H3 Prompt Studio plan total frame count is invalid.")
    if uses_project_assets:
        project_id = str(raw.get("project_id") or "")
        if not project_id or Path(project_id).name != project_id:
            raise ValueError("H3 Prompt Studio project asset plan has no valid project id.")
        if not str(raw.get("project_root") or "").strip():
            raise ValueError("H3 Prompt Studio project asset plan has no project root.")
    normalized = copy.deepcopy(raw)
    normalized["shots"] = normalized_shots
    expected_plan_hash = _stable_hash(
        {
            "compatibility": compatibility,
            "shots": [
                {key: value for key, value in shot.items() if key not in {"prompt", "scene_prompt"}}
                for shot in normalized_shots
            ],
        }
    )
    if normalized.get("plan_hash") != expected_plan_hash:
        raise ValueError("H3 Prompt Studio rule plan hash mismatch.")
    return normalized


class H3PromptStudioRulePlan:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "plan_json": (
                    "STRING",
                    {
                        "default": "{}",
                        "multiline": True,
                        "dynamicPrompts": False,
                        "tooltip": "只读派生计划。请在 Prompt Studio 修改源项目并重新下载工作流。",
                    },
                )
            }
        }

    RETURN_TYPES = (PLAN_TYPE, "STRING", "INT", "INT", "INT")
    RETURN_NAMES = ("plan", "summary", "clip_count", "width", "height")
    FUNCTION = "build"
    CATEGORY = "H3 Prompt Studio/rule loop"

    def build(self, plan_json: str):
        encoded = str(plan_json or "").encode("utf-8")
        if len(encoded) > MAX_PLAN_BYTES:
            raise ValueError("H3 Prompt Studio rule plan is too large.")
        try:
            raw = json.loads(encoded.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("H3 Prompt Studio rule plan JSON is invalid.") from exc
        plan = _validate_plan(raw)
        compatibility = plan["compatibility"]
        return (
            plan,
            str(plan.get("summary") or ""),
            len(plan["shots"]),
            int(compatibility["width"]),
            int(compatibility["height"]),
        )


class H3PromptStudioKeyframeRouter:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"state": (STATE_TYPE,)}}

    RETURN_TYPES = ("IMAGE", "IMAGE", "STRING")
    RETURN_NAMES = ("first_frame", "last_frame", "status")
    FUNCTION = "route"
    CATEGORY = "H3 Prompt Studio/rule loop"

    @classmethod
    def IS_CHANGED(cls, *args, **kwargs):
        return float("NaN")

    def route(self, state: dict[str, Any]):
        plan = state["plan"]
        index = int(state["index"])
        shot = plan["shots"][index - 1]
        width = int(plan["compatibility"]["width"])
        height = int(plan["compatibility"]["height"])
        first_spec = shot.get("first_frame")
        last_spec = shot.get("last_frame")
        first = None
        last = None
        if isinstance(first_spec, dict) and first_spec.get("source") == "previous_tail":
            previous = state.get("previous_frames")
            if previous is None or not torch.is_tensor(previous) or int(previous.shape[0]) < 1:
                raise ValueError(
                    "H3 Prompt Studio continuous shot %d has no previous delivered tail."
                    % index
                )
            first = _resize_frame(previous[-1:], width, height)
        elif isinstance(first_spec, dict) and first_spec.get("source") == "input":
            first = _load_input_image(str(first_spec.get("name") or ""), width, height)
        elif isinstance(first_spec, dict) and first_spec.get("source") == "project_asset":
            first = _load_project_asset(plan, first_spec, width, height)
        if isinstance(last_spec, dict) and last_spec.get("source") == "input":
            last = _load_input_image(str(last_spec.get("name") or ""), width, height)
        elif isinstance(last_spec, dict) and last_spec.get("source") == "project_asset":
            last = _load_project_asset(plan, last_spec, width, height)
        status = "scene %d %s: first=%s; last=%s" % (
            index,
            shot["mode"],
            (first_spec or {}).get("source", "none"),
            (last_spec or {}).get("source", "none"),
        )
        return (first, last, status)


class H3PromptStudioExactFrameMap:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "state": (STATE_TYPE,),
                "images": ("IMAGE",),
                "audio": ("AUDIO",),
            },
            "optional": {
                "first_frame": ("IMAGE",),
                "last_frame": ("IMAGE",),
            },
        }

    RETURN_TYPES = ("IMAGE", "AUDIO", "STRING")
    RETURN_NAMES = ("images", "audio", "status")
    FUNCTION = "map"
    CATEGORY = "H3 Prompt Studio/rule loop"

    def map(
        self,
        state: dict[str, Any],
        images: Any,
        audio: Any,
        first_frame: Any = None,
        last_frame: Any = None,
    ):
        if not torch.is_tensor(images) or images.ndim != 4:
            raise ValueError("H3 Prompt Studio native output must be an IMAGE batch.")
        plan = state["plan"]
        shot = plan["shots"][int(state["index"]) - 1]
        raw_frames = int(shot["raw_frames"])
        target_frames = int(shot["target_frames"])
        if int(images.shape[0]) != raw_frames:
            raise ValueError(
                "H3 Prompt Studio received %d native frames; the plan requires %d."
                % (int(images.shape[0]), raw_frames)
            )
        previous_tail = (shot.get("first_frame") or {}).get("source") == "previous_tail"
        indices = _uniform_indices(raw_frames, target_frames, previous_tail)
        index_tensor = torch.tensor(indices, dtype=torch.long, device=images.device)
        delivered = images.index_select(0, index_tensor).clone()
        width = int(plan["compatibility"]["width"])
        height = int(plan["compatibility"]["height"])
        if first_frame is not None and not previous_tail:
            replacement = _resize_frame(first_frame, width, height).to(
                device=delivered.device, dtype=delivered.dtype
            )
            delivered[0:1] = replacement
        if last_frame is not None:
            replacement = _resize_frame(last_frame, width, height).to(
                device=delivered.device, dtype=delivered.dtype
            )
            delivered[-1:] = replacement

        try:
            waveform = audio["waveform"]
            sample_rate = int(audio["sample_rate"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("H3 Prompt Studio decoded audio is invalid.") from exc
        if not torch.is_tensor(waveform) or sample_rate <= 0:
            raise ValueError("H3 Prompt Studio decoded audio is invalid.")
        wanted = int(round(target_frames / 24.0 * sample_rate))
        delivered_audio = {
            "waveform": _remap_audio(
                waveform,
                sample_rate=sample_rate,
                raw_frames=raw_frames,
                target_frames=target_frames,
                skip_first=previous_tail,
            ),
            "sample_rate": sample_rate,
        }
        if int(delivered.shape[0]) != target_frames or int(
            delivered_audio["waveform"].shape[-1]
        ) != wanted:
            raise ValueError("H3 Prompt Studio exact delivery contract failed.")
        status = (
            "scene %d: native %df -> exact %df; endpoints %d..%d; audio %d samples"
            % (
                int(state["index"]),
                raw_frames,
                target_frames,
                indices[0],
                indices[-1],
                wanted,
            )
        )
        return (delivered, delivered_audio, status)


class H3Idea2VideoProjectImageSave:
    """Save an IMAGE batch directly below this project's ignored runs folder."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "project_root": ("STRING", {"default": ""}),
                "relative_path": (
                    "STRING",
                    {
                        "default": "project/segments/0001/attempt_1/image.png",
                        "tooltip": "Path relative to the Idea2Video runs directory.",
                    },
                ),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("receipt_json",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "H3 Idea2Video/project output"

    @classmethod
    def IS_CHANGED(cls, *args, **kwargs):
        return float("NaN")

    def save(self, images: Any, project_root: str, relative_path: str):
        if not torch.is_tensor(images) or images.ndim != 4 or int(images.shape[0]) < 1:
            raise ValueError("Idea2Video project image output requires an IMAGE batch.")
        if not str(relative_path).lower().endswith(".png"):
            raise ValueError("Idea2Video project image output must use .png.")
        root, requested = _project_destination(project_root, relative_path)
        from PIL import Image
        import numpy as np

        count = int(images.shape[0])
        destinations = [
            requested
            if count == 1
            else requested.with_name(f"{requested.stem}_{index:04d}.png")
            for index in range(1, count + 1)
        ]
        written: list[Path] = []
        try:
            for batch_index, destination in enumerate(destinations):
                temporary = destination.with_name(
                    f".{destination.stem}.{uuid.uuid4().hex}.tmp.png"
                )
                pixels = 255.0 * images[batch_index].detach().cpu().numpy()
                image = Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))
                image.save(temporary, format="PNG", compress_level=4)
                os.replace(temporary, destination)
                written.append(destination)
        except Exception:
            for destination in destinations:
                for temporary in destination.parent.glob(f".{destination.stem}.*.tmp.png"):
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
            raise
        return _node_result(_receipt("image", root, written))


class H3Idea2VideoProjectVideoSave:
    """Encode a Comfy VIDEO directly to one deterministic project MP4."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video": ("VIDEO",),
                "project_root": ("STRING", {"default": ""}),
                "relative_path": (
                    "STRING",
                    {
                        "default": "project/segments/0001/attempt_1/video.mp4",
                        "tooltip": "Path relative to the Idea2Video runs directory.",
                    },
                ),
                "crf": ("FLOAT", {"default": 18.0, "min": 0.0, "max": 51.0, "step": 1.0}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("receipt_json",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "H3 Idea2Video/project output"

    @classmethod
    def IS_CHANGED(cls, *args, **kwargs):
        return float("NaN")

    def save(self, video: Any, project_root: str, relative_path: str, crf: float = 18.0):
        if not str(relative_path).lower().endswith(".mp4"):
            raise ValueError("Idea2Video project video output must use .mp4.")
        root, destination = _project_destination(project_root, relative_path)
        temporary = destination.with_name(
            f".{destination.stem}.{uuid.uuid4().hex}.tmp.mp4"
        )
        try:
            from comfy_api.latest import Types

            video.save_to(
                str(temporary),
                format=Types.VideoContainer("mp4"),
                codec="h264",
                metadata=None,
                crf=float(crf),
            )
            os.replace(temporary, destination)
        except Exception:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
        return _node_result(_receipt("video", root, [destination]))


class H3Idea2VideoProjectFileCopy:
    """Copy one verified Comfy output artifact into the project runs folder."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "source_path": ("STRING", {"forceInput": True}),
                "project_root": ("STRING", {"default": ""}),
                "relative_path": (
                    "STRING",
                    {"default": "project/context_loop/output/native.mp4"},
                ),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("receipt_json",)
    FUNCTION = "copy"
    OUTPUT_NODE = True
    CATEGORY = "H3 Idea2Video/project output"

    @classmethod
    def IS_CHANGED(cls, *args, **kwargs):
        return float("NaN")

    def copy(self, source_path: str, project_root: str, relative_path: str):
        import folder_paths

        source = Path(str(source_path or "")).resolve(strict=True)
        output_root = Path(folder_paths.get_output_directory()).resolve(strict=True)
        try:
            source.relative_to(output_root)
        except ValueError as exc:
            raise ValueError("Idea2Video copy source is outside ComfyUI output.") from exc
        if not source.is_file():
            raise ValueError("Idea2Video copy source is not a file.")
        root, destination = _project_destination(project_root, relative_path)
        temporary = destination.with_name(
            f".{destination.name}.{uuid.uuid4().hex}.part"
        )
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary, destination)
        except Exception:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
        return _node_result(_receipt("copy", root, [destination]))


NODE_CLASS_MAPPINGS = {
    "H3PromptStudioRulePlan": H3PromptStudioRulePlan,
    "H3PromptStudioKeyframeRouter": H3PromptStudioKeyframeRouter,
    "H3PromptStudioExactFrameMap": H3PromptStudioExactFrameMap,
    "H3Idea2VideoProjectImageSave": H3Idea2VideoProjectImageSave,
    "H3Idea2VideoProjectVideoSave": H3Idea2VideoProjectVideoSave,
    "H3Idea2VideoProjectFileCopy": H3Idea2VideoProjectFileCopy,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "H3PromptStudioRulePlan": "H3 Prompt Studio Rule Plan (Read-only)",
    "H3PromptStudioKeyframeRouter": "H3 Prompt Studio Keyframe Router",
    "H3PromptStudioExactFrameMap": "H3 Prompt Studio Exact Frame Map",
    "H3Idea2VideoProjectImageSave": "H3 Idea2Video Save Image To Project",
    "H3Idea2VideoProjectVideoSave": "H3 Idea2Video Save Video To Project",
    "H3Idea2VideoProjectFileCopy": "H3 Idea2Video Copy Artifact To Project",
}
