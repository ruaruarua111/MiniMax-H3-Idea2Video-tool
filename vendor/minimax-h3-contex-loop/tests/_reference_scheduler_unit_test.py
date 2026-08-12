#!/usr/bin/env python3
"""Standalone scheduler compiler test without importing a ComfyUI checkout."""

import importlib.util
import json
import pathlib
import sys
import tempfile
import types


ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = "h3_reference_scheduler_unit"

folder_paths = types.ModuleType("folder_paths")
folder_paths.get_output_directory = lambda: str(ROOT)
folder_paths.get_temp_directory = lambda: str(ROOT)
folder_paths.get_input_directory = lambda: str(ROOT)
folder_paths.get_annotated_filepath = lambda value: str(value)
sys.modules["folder_paths"] = folder_paths

package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package

shared_nodes = types.ModuleType(PACKAGE + ".nodes")
shared_nodes.MiniMaxH3MotionContext = object
shared_nodes._claim_inline_patch_ownership = lambda: "test patch owner"
shared_nodes._prepare_native_guide_conditioning = lambda *args: None
shared_nodes._resize = lambda *args: None
shared_nodes._streams_from_latent = lambda *args: None
sys.modules[shared_nodes.__name__] = shared_nodes

spec = importlib.util.spec_from_file_location(
    PACKAGE + ".chain_nodes", ROOT / "chain_nodes.py")
chain = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = chain
spec.loader.exec_module(chain)


class LazyAudio:
    """Minimal non-dict ComfyUI AUDIO proxy for compatibility testing."""

    def __init__(self, value):
        self.value = value
        self.reads = 0

    def __getitem__(self, key):
        self.reads += 1
        return self.value[key]

def schedule():
    return chain._make_reference_schedule([
        {
            "kind": "picture", "tag": "hero_face", "scenes": "1:7",
            "ranges": ((1, 7),), "value": object(), "content_hash": "face",
            "declaration": "THIS LEGACY TEXT MUST NEVER BE INSERTED",
        },
        {
            "kind": "picture", "tag": "hero_look", "scenes": "all",
            "ranges": (), "value": object(), "content_hash": "look",
        },
        {
            "kind": "video", "tag": "performance", "scenes": "4:6",
            "ranges": ((4, 6),), "value": object(), "audio": object(),
            "audio_tag": "performance_audio", "content_hash": "video",
            "audio_hash": "paired-audio",
            "audio_declaration": "NOR THIS LEGACY TEXT",
        },
        {
            "kind": "audio", "tag": "song", "scenes": "all",
            "ranges": (), "value": object(), "content_hash": "song",
        },
    ])


workflow = json.loads((
    ROOT / "example_workflows" /
    "Looping MiniMax H3 Seamless Chain V2 - Scheduled Refs.json"
).read_text(encoding="utf-8"))
plan_node = next(node for node in workflow["nodes"]
                 if node.get("type") == "MiniMaxH3ChainPlan")
plan = json.loads(plan_node["widgets_values"][0])

for scene in (1, 4, 8):
    source = "\n".join(plan["shots"][scene - 1]["prompt"])
    compiled, mapping, _bindings = chain._compile_scheduled_reference_prompt(
        schedule(), scene, 14, source)
    assert "@hero" not in compiled
    assert "@performance" not in compiled
    assert "@song" not in compiled
    assert "LEGACY TEXT" not in compiled
    assert "{ref}" not in compiled
    assert compiled.startswith("subject_definitions:\n<Subject 1>")
    if scene == 1:
        assert "<Picture 1>" in compiled and "<Picture 2>" in compiled
        assert "<Audio 1> is the current frame-exact" in compiled
    elif scene == 4:
        assert "<Video 1> provides a weak reference" in compiled
        assert "<Audio 1> is the synchronized soundtrack" in compiled
        assert "<Audio 2> is the current frame-exact" in compiled
    else:
        assert "defined by <Picture 1>" in compiled
        assert "<Picture 2>" not in compiled
        assert "<Audio 1> is the current frame-exact" in compiled
    assert mapping.startswith("scene %d/14:" % scene)

picture_inputs = chain.MiniMaxH3ScheduledPictureReference.INPUT_TYPES()[
    "required"]
video_inputs = chain.MiniMaxH3ScheduledVideoReference.INPUT_TYPES()["required"]
audio_inputs = chain.MiniMaxH3ScheduledAudioReference.INPUT_TYPES()["required"]
assert "declaration" not in picture_inputs
assert "declaration" not in video_inputs
assert "audio_declaration" not in video_inputs
assert "declaration" not in audio_inputs

lazy_audio = LazyAudio({
    "waveform": chain.torch.zeros((1, 2, 8000), dtype=chain.torch.float32),
    "sample_rate": 8000,
})
lazy_schedule, lazy_fingerprint, lazy_status = (
    chain.MiniMaxH3ScheduledAudioReference().add(
        lazy_audio, "lazy_voice", "1:2"))
assert lazy_audio.reads > 0
assert lazy_schedule["entries"][0]["value"] is lazy_audio
assert len(lazy_schedule["entries"][0]["content_hash"]) == 64
assert lazy_schedule["fingerprint"] == lazy_fingerprint
assert "@lazy_voice audio on 1:2" in lazy_status
try:
    chain.MiniMaxH3ScheduledAudioReference().add(
        None, "missing_voice", "1")
except ValueError as exc:
    message = str(exc)
    assert "received no audio (None)" in message
    assert "source_audio_slice" in message
    assert "generated_audio" in message
    assert "connect Load Audio directly" in message
    assert "source_plus_timeline" in message
    assert "muted or bypassed" in message
    assert "playable browser preview" in message
else:
    raise AssertionError("missing scheduled audio was accepted")
try:
    chain.MiniMaxH3ScheduledAudioReference().add(
        lambda: b"legacy VHS_AUDIO", "legacy", "1")
except ValueError as exc:
    assert "ComfyUI AUDIO" in str(exc)
else:
    raise AssertionError("legacy callable VHS_AUDIO was accepted")

plan_inputs = chain.MiniMaxH3ChainPlan.INPUT_TYPES()["required"]
audio_mode_help = plan_inputs["audio_mode"][1]["tooltip"]
assert "does NOT enable or disable @voice/<Audio N> references" in audio_mode_help
assert "finished prerecorded voice" in audio_mode_help
assert "short @voice identity/timbre reference" in audio_mode_help
assert "generated_audio" in audio_mode_help
assert "experimental" in audio_mode_help
assert "output/h3_chains" in plan_inputs["run_name"][1]["tooltip"]
base_seed_help = plan_inputs["base_seed"][1]["tooltip"]
assert "Reroll seed does NOT change base_seed" in base_seed_help
assert "always-visible Scene seed" in base_seed_help
assert "audio_tag" in video_inputs
conditioning = object()
priority_result = chain.MiniMaxH3PatchPriority().claim(conditioning)
assert priority_result == (conditioning, "test patch owner")

original_output_root = chain._output_root
original_launch_directory = chain._launch_directory
try:
    with tempfile.TemporaryDirectory() as output_root:
        opened_paths = []
        chain._output_root = lambda: output_root
        chain._launch_directory = lambda path: (
            opened_paths.append(path) or True, None)
        folder_result = chain._open_run_output_directory("Project Name")
        expected_folder = pathlib.Path(
            output_root, "h3_chains", "Project_Name")
        assert folder_result["opened"] is True
        assert pathlib.Path(folder_result["path"]) == expected_folder
        assert expected_folder.is_dir()
        assert opened_paths == [str(expected_folder)]
        chain._launch_directory = lambda _path: (False, "headless host")
        fallback_result = chain._open_run_output_directory("Project Name")
        assert fallback_result["opened"] is False
        assert fallback_result["error"] == "headless host"
        try:
            chain._open_run_output_directory("../../")
        except ValueError as exc:
            assert "run_name" in str(exc)
        else:
            raise AssertionError("unsafe empty run_name was accepted")
finally:
    chain._output_root = original_output_root
    chain._launch_directory = original_launch_directory

i2va_workflow = json.loads((
    ROOT / "example_workflows" /
    "Looping MiniMax H3 V2 - Single Image I2VA 20s.json"
).read_text(encoding="utf-8"))
i2va_plan_node = next(node for node in i2va_workflow["nodes"]
                       if node.get("type") == "MiniMaxH3ChainPlan")
normalized = chain.MiniMaxH3ChainPlan().build(
    *i2va_plan_node["widgets_values"])[0]
assert [shot["raw_frames"] for shot in normalized["shots"]] == [243, 243]
assert [shot["delivered_frames"] for shot in normalized["shots"]] == [243, 238]
assert normalized["total_delivered_frames"] == 481
assert normalized["total_delivered_frames"] / chain.FPS > 20
assert normalized["compatibility"]["context_length"] == 5
assert "<Picture 1>" in normalized["shots"][0]["scene_prompt"]
assert "<Picture" not in normalized["shots"][1]["scene_prompt"]

gate_node = next(node for node in i2va_workflow["nodes"]
                 if node.get("type") == "MiniMaxH3ChainFirstSceneImage")
assert gate_node["inputs"][0]["name"] == "state"
assert gate_node["inputs"][1]["name"] == "image"
opening_image = object()
gate = chain.MiniMaxH3ChainFirstSceneImage()
first_result = gate.select({"index": 1}, opening_image)
later_result = gate.select({"index": 2}, opening_image)
assert first_result[:2] == (opening_image, True)
assert later_result[:2] == (None, False)

links = {link[0]: link for link in i2va_workflow["links"]}
nodes = {node["id"]: node for node in i2va_workflow["nodes"]}
for node in nodes.values():
    for slot, input_spec in enumerate(node.get("inputs", [])):
        link_id = input_spec.get("link")
        if link_id is None:
            continue
        assert link_id in links
        assert links[link_id][3:5] == [node["id"], slot]
    for slot, output_spec in enumerate(node.get("outputs", [])):
        for link_id in output_spec.get("links") or []:
            assert link_id in links
            assert links[link_id][1:3] == [node["id"], slot]
i2v_node = next(node for node in nodes.values()
                 if node.get("type") == "MiniMaxH3ImageToVideo")
assert next(item for item in i2v_node["inputs"]
            if item["name"] == "first_frame")["link"] is not None
assert next(item for item in i2v_node["inputs"]
            if item["name"] == "last_frame")["link"] is None

print("H3 scheduler: aliases, Plan guidance, and looping I2VA workflow pass")
