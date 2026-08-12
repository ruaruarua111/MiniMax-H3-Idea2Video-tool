#!/usr/bin/env python3
"""Standalone regression test for preserving H3 audio beside source-audio finals."""

import importlib.util
import pathlib
import sys
import tempfile
import types
import wave

import torch


ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = "h3_generated_audio_sidecar_unit"

folder_paths = types.ModuleType("folder_paths")
folder_paths.get_output_directory = lambda: folder_paths.output_directory
folder_paths.get_temp_directory = lambda: folder_paths.output_directory
folder_paths.get_input_directory = lambda: folder_paths.output_directory
folder_paths.get_annotated_filepath = lambda value: str(value)
folder_paths.output_directory = str(ROOT)
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


def audio(value):
    return {
        "waveform": torch.full(
            (1, 2, round(5 / chain.FPS * 8000)), value,
            dtype=torch.float32),
        "sample_rate": 8000,
    }


def main():
    with tempfile.TemporaryDirectory() as tempdir:
        folder_paths.output_directory = tempdir
        segment_path = pathlib.Path(
            tempdir, "h3_chains", "sidecar", "segments", "clip_0001.mp4")
        segment_path.parent.mkdir(parents=True)
        segment_path.write_bytes(b"fake H.264 segment")
        manifest = {
            "format": "h3_chain_manifest_v3",
            "run_name": "sidecar",
            "compatibility": {"audio_mode": "source_track"},
            "clip_count": 1,
            "total_delivered_frames": 5,
            "segments": [{
                "index": 1,
                "segment": chain._relative_output_path(str(segment_path)),
                "delivered_frames": 5,
            }],
        }
        generated = audio(0.25)
        source = audio(0.0)
        muxed_pcm = []

        chain._validate_manifest = lambda value: value["segments"]
        chain._validate_prelude = lambda _value: None
        chain._generated_audio = lambda _value: generated
        chain._validate_source_audio_hash = lambda *_args: None
        chain._manifest_media_metadata = lambda _value: {}
        original_which = chain.shutil.which
        chain.shutil.which = lambda executable: (
            "/fake/ffmpeg" if executable == "ffmpeg"
            else original_which(executable))

        def fake_ffmpeg(command, timeout_seconds=None):
            del timeout_seconds
            output = pathlib.Path(command[-1])
            if output.name == ".final.tmp.mp4":
                with wave.open(command[5], "rb") as selected_audio:
                    muxed_pcm.append(selected_audio.readframes(
                        selected_audio.getnframes()))
            output.write_bytes(b"assembled video")

        chain._run_ffmpeg = fake_ffmpeg
        try:
            result = chain.MiniMaxH3ChainAssemble().assemble(
                manifest, "source", "source_final", 96, source)
        finally:
            chain.shutil.which = original_which

        final_path = pathlib.Path(result["result"][0])
        sidecar_path = final_path.with_suffix(".generated.wav")
        assert final_path.is_file()
        assert sidecar_path.is_file()
        assert muxed_pcm and not any(muxed_pcm[0])
        with wave.open(str(sidecar_path), "rb") as generated_audio:
            assert generated_audio.getframerate() == 8000
            assert generated_audio.getnchannels() == 2
            assert generated_audio.getnframes() == round(5 / chain.FPS * 8000)
            assert any(generated_audio.readframes(generated_audio.getnframes()))
        assert "generated audio ->" in result["ui"]["text"][0]

    print("H3 generated audio sidecar: source mux remains unchanged and H3 WAV is preserved")


if __name__ == "__main__":
    main()
