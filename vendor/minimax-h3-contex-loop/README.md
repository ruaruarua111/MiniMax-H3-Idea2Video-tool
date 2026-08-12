<p align="center">
  <img src="assets/minimax-h3-contex-loop.svg" alt="MiniMax H3 Contex Loop — scene plans that survive the render" width="100%">
</p>

# ComfyUI MiniMax H3 Contex Loop

Turn one MiniMax H3 sampling body into a scene-by-scene production loop. Every
accepted scene carries motion and optional audio forward, saves a checkpoint,
can be reviewed or retried, and joins into the final video without building a
huge cumulative image tensor.

[Prompt & timing guide](H3_CHAIN_FORMAT_GUIDE.md) ·
[Example workflows](example_workflows/README.md) ·
[Third-party credits](THIRD_PARTY_NOTICES.md)

> **Contex** is the intentional public repository spelling.

## Changelog

Newest first. Recent additions stay visible; older milestones are folded away
so this page remains a useful starting point rather than a changelog wall.

- **v0.3.20 — Cancel and reroll the active scene.** While an H3 scene is
  generating, a guarded floating action can cancel only that prompt, assign a
  new explicit scene seed, move Loop Start to the interrupted scene, preserve
  the selected range end, and requeue through ComfyUI's normal queue. Later
  scenes require their preceding checkpoint; Review Gate keeps ownership once
  generation has finished.
- **v0.3.19 — Plan and review UX pass.** Plan controls retain pointer input,
  editor and preview sizes persist, scene seeds are always visible with derived
  and random controls, and reference menus show only sources active in the
  selected scene. Documentation now states that `@aliases` are optional.
- **v0.3.14 — Explicit compatible patch priority.** The optional wired
  **MiniMax H3 Patch Priority** pass-through can promote this pack over an
  older compatible H3 Motion Context patch copy. It leaves conditioning
  unchanged, retains recognised H3-Multishot and SolAttn behavior, and refuses
  unknown wrappers rather than overwriting them.
- **v0.3.13 — Open a Plan's output folder.** A compact **Output** button with
  an outline folder-open icon in the Plan header creates and opens
  `output/h3_chains/<run_name>` on the ComfyUI host. Headless or systemd-hosted
  servers fall back to copying the exact host path into the browser clipboard.
- **v0.3.12 — Clearer Plan guidance and looping I2VA.** Expanded every ambiguous Plan tooltip,
  including a direct choice between exact prerecorded voice/song tracks,
  short voice-identity references with generated speech, and the experimental
  mixed audio mode. Seed rerolls now explicitly point users to the per-scene
  override rather than `base_seed`. A dedicated single-image I2VA example and
  First-Scene Image Gate now anchor only scene 1 before context-only recursive
  continuations.
- **v0.3.11 — Invisible legacy widget-width repair.** While any Contex Loop
  node is on the canvas, the pack repairs the LiteGraph widget-width regression
  across every node, including nodes from other packs. No dedicated fix node is
  required, and a ComfyUI compatibility setting can disable the workaround.
  Regenerated scenes now retain every previous segment/checkpoint revision
  instead of deleting the superseded take.
- **v0.3.10 — Scene-scheduled Ref2VA.** Chain picture, video, paired-video
  audio, and standalone-audio references under stable `@tags`; each scene
  receives only its active sockets while native `<Picture N>`, `<Video N>`,
  and `<Audio N>` labels compile automatically. Reference definitions remain
  visible and editable in the Plan/Prompt Editor. A right-click
  converter turns an already-wired core Ref2VA node into this layout.
- **v0.3.8 — One-pass performance re-filming.** A Reference Video Prep node
  converts native VIDEO or decoded IMAGE/AUDIO to exact 24 fps Ref2VA input,
  copies its soundtrack without padding or time-stretching, and powers a new
  experimental three-angle guitar workflow.
- **v0.3.7 — Flexible video loaders.** Existing Video Context now accepts a
  native ComfyUI `VIDEO` directly or separate `IMAGE + AUDIO + FPS` outputs
  from VHS and other decoding nodes.
- **v0.3.6 — Extend an existing video.** A typed adapter turns decoded video
  and optional audio into scene 1 context, while optional prepend preserves the
  normalized original before partial or final assembled output.
- **v0.3.5 — Native guides and portable assembly.** Automatically uses
  ComfyUI’s native arbitrary-position AV guides when PR #15439 (or its merged
  equivalent) is present, retains the guarded legacy path, and falls back to
  PyAV review and stream-copy assembly when no `ffmpeg` executable is available.
- **v0.3.4 — Scene Prompt Editor.** A synchronized, large-format companion for
  editing each scene’s real Plan prompt, with scene navigation, reference and
  dialogue shortcuts, and adjustable type size.
- **v0.3.3 — Reliable preview resizing.** Review video sizing now remains stable
  when the ComfyUI canvas is zoomed.
- **v0.3.2 — Resizable review video.** Drag the bar beneath Review Gate’s player
  to give the preview more or less room.
- **v0.3.1 — Friendlier JSON defaults.** Top-level `duration_seconds` and `steps`
  shorthand now populate the visual Plan defaults correctly.
- **v0.3.0 — Archival PNG export.** Re-decode saved scene checkpoints into a
  continuous lossless PNG sequence without holding the whole production in RAM.

<details>
<summary><strong>v0.2.0 — Recovery, metadata, and compatibility</strong></summary>

- Persisted each scene prompt, effective plan, workflow, and API prompt beside
  the rendered chain.
- Added scene-range rendering, resumable review checkpoints, partial assembly,
  notification/timeout controls, and Firefox-safe Review Gate recovery.
- Added guarded compatibility with H3-Multishot, SolAttn, Ref2VA, and the
  separately installable upstream H3 Motion Context pack.
- Added Comfy Registry publishing and the shorter project-focused README.

</details>

<details>
<summary><strong>v0.1.0 — The production loop takes shape</strong></summary>

- Introduced the visual scene-plan editor, readable multiline prompts, automatic
  scene colors, responsive layout, and collapsible raw JSON.
- Added the recursive one-body chain, frame-locked audio trimming, per-scene
  checkpoints, interactive review/retry, and the looping Ref2VA example.
- Renamed the expanded project **MiniMax H3 Contex Loop** so it can coexist
  clearly with NikoDemon80’s original manual Motion Context tools.

</details>

<details>
<summary><strong>Origins — Motion Context and Ref2VA continuation</strong></summary>

- Began with MiniMax H3 clip chaining and true generated-audio continuation.
- Added motion-context support for H3 Ref2VA, followed by opt-in compatibility
  patches and a resumable disk-backed loop.

</details>

## Why this project has its own name

This work began with **NikoDemon80’s** excellent
[H3 Motion Context](https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context).
As it grew from a continuation experiment into a full scene planner, recursive
renderer, review gate, checkpoint system, and recovery workflow, sharing the
same identity stopped being helpful.

**MiniMax H3 Contex Loop** gives both projects room to be clear: Niko’s pack
stays focused on manual Motion Context tools, while this one can evolve around
long, reviewable productions. Their node IDs are separate, both can be installed
together, and existing `output/h3_chains/` checkpoints remain valid. It is a
new lane, not an erased history—the original research and commit lineage stay
credited.

The Ref2VA multi-reference/audio fix and first global-ref demo were contributed
by **seitanism**. The editor’s quick reference/dialogue interactions were
inspired by **nkxx188’s**
[ComfyUI-MiniMaxH3-Easy](https://github.com/nkxx188/ComfyUI-MiniMaxH3-Easy).

## What you get

| | Feature |
|---|---|
| 🎬 | Visual multiline scene planner with exact H3 timing |
| 🔁 | One recursive sampling body for the whole plan |
| 🧬 | Motion and optional audio continuity |
| 👀 | Video-with-sound review, edit, reroll, or approve |
| 💾 | Atomic checkpoints, partial assembly, and resume |
| 🖼️ | Re-decode saved latents into a continuous PNG sequence |
| 🎯 | Scene ranges such as `3` or `3:8` |
| ⏩ | Continue an existing video, with optional original-video prepend |
| 🎸 | Re-film one synchronized performance from new camera angles |
| 🗓️ | Schedule Ref2VA sources per scene with stable human-readable tags |
| 🖼️ | Apply one I2VA opening image only to scene 1 of a long chain |

The runtime changes are opt-in. Loading this pack does not alter ordinary
ComfyUI workflows; its guarded patches activate only when a Contex Loop Context
node executes and self-test before touching H3 conditioning. The frontend
widget-width compatibility layer likewise activates only while a node from this
pack is present on the canvas.

## Install

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/ethanfel/ComfyUI-MiniMaxH3-Contex-Loop.git
```

Restart ComfyUI and hard-refresh the browser. Niko’s upstream pack is optional;
install it alongside this one if you also want its manual Motion Context,
Save Latent, and Load Latent nodes. H3-Multishot can also remain installed; its
AV-bank payload merge is detected and reused without stacking another wrapper.

An `ffmpeg` executable on `PATH` is preferred for review and final assembly.
When it is unavailable, Review Gate and Assemble automatically use ComfyUI’s
bundled PyAV: saved H.264 video packets are stream-copied without quality loss
and selected audio is encoded to AAC.

## Start here

Start with one of the three v2 workflows:

- [Single-image I2VA 20s v2](<example_workflows/Looping MiniMax H3 V2 - Single Image I2VA 20s.json>)
  is the simplest long-form image-to-video example: one opening image, no last
  frame, and two requested 10-second scenes. Its First-Scene Image Gate applies
  `<Picture 1>` only to scene 1; scene 2 continues from motion context. With a
  five-frame overlap, the assembled result is 481 frames, or 20.04 seconds.
- [Core FL2VA v2](<example_workflows/Looping MiniMax H3 V2 - Core FL2VA.json>)
  uses ComfyUI's stock `MiniMaxH3ImageToVideo` with no reference scheduler. It
  is a one-scene first/last-frame model that also demonstrates the large prompt
  editor, native keyframe hover previews, review/retry, recovery, and safe
  date-versioned assembly.
- [Scheduled Ref2VA v2](<example_workflows/Looping MiniMax H3 Seamless Chain V2 - Scheduled Refs.json>)
  is the complete long-form loop with scene-selective picture, video, paired
  audio, and frame-exact song references. Its in-canvas notes show every alias
  mapping used by the fourteen-scene plan.

The common loop remains deliberately small:

```text
Plan → Loop Start → Current Shot → stock H3 conditioning
                                      ↓
                               Contex Loop Context
                                      ↓
                           sample → decode → Loop Trim
                                      ↓
                     Segment + Checkpoint → Review Gate
                                      ↓
                                  Loop End ──↺

Loop End manifest → Assemble
```

The Assemble `filename` field accepts ComfyUI-style date tokens such as
`%date:yyyy-MM-dd%`, along with `%year%`, `%month%`, `%day%`, `%hour%`,
`%minute%`, and `%second%`. Assemble preserves existing exports: when the
requested MP4 already exists, the next file receives `_001`, `_002`, and so on
instead of replacing it.

To vary references by scene, replace the stock Ref2VA conditioning node with
**MiniMax H3 Scheduled Ref2VA** and build its typed schedule:

```text
Load Image ─→ Scheduled Picture Ref ─┐
24 fps IMAGE (+ paired AUDIO) ─→ Scheduled Video Ref ─┐
Standalone AUDIO ─→ Scheduled Audio Ref ──────────────┴→ Scheduled Ref2VA

Current Shot prompt / clip_index / clip_count / width / height / length ───↗
CLIP + video VAE + audio VAE ─────────────────────────────────────────────↗
```

Each entry can have a stable tag such as `@hero`, `@performance`, or `@voice`.
Its `scenes` field accepts blank/all, `1`, `1:4`, or `1,3,5:8`. The wrapper
passes only active media into core Ref2VA and replaces tags with that scene's
native labels. It never inserts or rewrites semantic prompt text; write every
reference definition directly in the Plan or Prompt Editor.

Aliases are optional authoring conveniences, not extra H3 syntax the user must
adopt. Core Ref2VA workflows can keep writing native `<Picture N>`, `<Video N>`,
and `<Audio N>` labels. Scheduled workflows may also use native labels when the
author deliberately manages each scene's compact numbering; `@tags` are simply
the safer option when references appear, disappear, or renumber between scenes.

Tags identify references; their names do not reserve native H3 numbers. Native
labels are compactly assigned by media type from the entries active in the
current scene. For example, suppose two picture nodes use tags `picture_1` and
`picture_2`. If `picture_1` is removed or inactive in scene 3, write
`@picture_2` in that scene's prompt: the wrapper safely compiles it to
`<Picture 1>`. The `active_references` output shows the exact mapping for the
current scene.

The **Scene Prompt Editor** discovers the Scheduled Ref2VA connected downstream
without adding an execution socket or a graph cycle. Open its **@ Reference**
tray to see only references connected and active for the selected scene, with
stable tags and their native mapping when using the scheduler.
Hover a tag to preview an upstream loaded image, video, or audio file, then
click it to insert the stable `@tag`. Audio uses visible playback controls and
never autoplays. A computed media tensor can still be scheduled, but its hover
preview is available only when the editor can trace it to a browser-playable
upstream file.

The same tray also recognizes a downstream core **MiniMax H3 Reference to
Video** node. In that compatibility mode it previews connected media and
inserts native labels such as `<Picture 1>` and `<Audio 1>`; stable `@tags` and
scene scheduling remain exclusive to Scheduled Ref2VA.

Core **MiniMax H3 Image to Video** is recognized as well. With both keyframes
connected, the tray exposes hoverable `<Picture 1>` (first frame) and
`<Picture 2>` (last frame). With only the last frame connected for L2VA, it is
correctly presented as `<Picture 1>`.

For static references, connect the final schedule fingerprint to the Plan's
`generation_fingerprint` so changed media, tags, or selectors invalidate resume.
When an entry consumes a Current Shot output such as `source_audio_slice`, keep
that entry inside the loop and do not create a fingerprint cycle back to Plan;
the Plan already fingerprints the full source track.

Already have a core **MiniMax H3 Reference to Video** wired? Right-click it and
choose **Convert to MiniMax H3 Scheduled Ref2VA**. The converter preserves its
CLIP/VAE, prompt, dimensions, length, picture/video/audio sources, paired video
soundtracks, and conditioning/latent consumers. It also connects Current Shot's
scene index/count when it can identify the loop. The original core node remains
untouched in ComfyUI; the replacement invokes it internally per scene.

If another installed pack vendors an older version of the same H3 compatibility
patch, insert **MiniMax H3 Patch Priority** between Ref2VA/I2V conditioning and
**Contex Loop Context**. Because it is wired, it executes before continuation
guides are added and promotes this pack's implementation for the ComfyUI
process. It only replaces a recognised H3 Motion Context sibling; compatible
H3-Multishot/SolAttn hooks remain active and an unknown wrapper produces a clear
error rather than being overwritten. Leaving the node absent or disconnected
does not change runtime behavior.

For a non-looping experiment, open the
[three-angle guitar Ref2VA workflow](<example_workflows/EXPERIMENTAL MiniMax H3 Three-Angle Guitar Ref2VA.json>).
It loads `3ClbaJYWVO4_000030.mp4`, turns the source performance into a
209-frame synchronized Ref2VA reference, generates three alternate viewpoints
in one pass, and exports with the original waveform cut exactly to 8.708 s.
The source product card and watermark are deliberately excluded by the prompt.

To extend an existing video, add **MiniMax H3 Existing Video Context**:

The ready-to-run wiring is included separately in the
[experimental existing-video model workflow](<example_workflows/MiniMax H3 Extend Existing Video Model Workflow.json>).
It uses core Load Video directly, generated-audio continuity, optional
original-video prepend, and a Review Gate between every saved scene and Loop
End. This path is new and should be treated as experimental while it receives
broader real-world validation. The earlier examples are unchanged.

```text
Core Load Video (VIDEO) ─────────────→ Existing Video Context ─→ Loop Start
Other loader IMAGE + AUDIO + FPS ────↗
Plan ────────────────────────────────↗
H3 audio VAE ─────────────────────────────────────→ Loop Context (optional)
```

Connect either `source_video` or `source_frames`, never both. Native `VIDEO`
provides its own decoded frames, embedded audio, and exact frame rate; an
explicit `source_audio` overrides its embedded audio. For IMAGE-based loaders,
wire their frames and optional audio, then set or connect `source_fps` to the
actual decoded-frame rate. The adapter normalizes either route to the Plan
canvas and H3's 24 fps, then uses its final `context_length` frames as scene
1's predecessor. With `head` mode its repeated context is removed by Loop Trim.
Connect the H3 audio VAE to Loop Context when carrying imported audio in
`generated_audio` or `source_plus_timeline` mode.

With `prepend_original=true`, the normalized source is saved once under the run
folder and Assemble automatically places it before generated scenes, including
partial videos. Its audio is followed by the selected extension audio. Disable
prepend to produce only the extension. Since arbitrary input codecs, frame
rates, and sizes cannot be stream-concatenated safely, the preserved source is
re-encoded at the Plan's `segment_crf`; generated H.264 scenes remain
stream-copied without another quality pass.

Recommended first settings:

```text
context_length       22
encode_mode          video
anchor_mode          head
audio_context_length 22
Loop Trim match_tail true
Spectrum             off
```

Use this pack’s **MiniMax H3 Contex Loop Trim** after decoding. With
`match_tail=true`, it removes repeated leading context and corrects H3’s
fractional audio-step difference by truncating or zero-padding the final few
milliseconds.

## Scene plans

The Plan node provides a visual editor and stores ordinary JSON underneath.
Shared instructions belong in `prompt_prefix`; each scene only describes what
changes.

```json
{
  "prompt_prefix": "Keep the same performer, wardrobe and visual language.",
  "defaults": {"duration_seconds": 15, "steps": 20},
  "shots": [
    {"id": "intro", "prompt": "Instrumental opening in the elevator.", "seed": 123},
    {"id": "street", "prompt": "Continue outside into the rain.", "seed": 456}
  ]
}
```

Prompts may be multiline strings or arrays of lines. Using seconds lets the Plan
node handle H3’s `17k+5` frame grid; raw JSON remains available for
copy/import/export.

For long-form writing, connect the Plan output to **MiniMax H3 Scene Prompt
Editor**. Its large textarea edits the selected scene's real `shots[n].prompt`
inside the connected Plan—there is no duplicate prompt storage. Use the arrow
buttons or `Alt+Left/Right` to move between scenes, `@` for Picture/Video/Audio
reference tags, `#` for dialogue tags, and `A−`/`A+` for a persistent font size.
The node may sit inline before Loop Start or on an editor-only branch.

### Prompt Assistant (Codex or Hermes)

> **Currently disabled:** the embedded Prompt Assistant UI is dormant so the
> Scene Prompt Editor retains its original compact manual-editing experience.
> The implementation and tests remain in the repository for a future revisit;
> use the comfyui-mcp sidebar Agent panel for prompt assistance in the meantime.

When enabled, the Scene Prompt Editor contains an optional **Prompt Assistant**.
It uses the local `comfyui-mcp` panel orchestrator as a bridge, so agent
authentication and execution stay on the machine where Codex or Hermes is
installed. Start the current prompt-assist-capable `comfyui-mcp` build with:

```bash
comfyui-mcp --panel-orchestrator
```

Then, inside the node:

1. choose **Codex** or **Hermes** and an action such as Rewrite, Continuity,
   Shorten, Critique, or Discuss;
2. optionally include the shared prompt and previous/next scenes, select a
   passage if it deserves special attention, and type your instruction;
3. press **Ask agent** (`Ctrl/Cmd+Enter` also sends);
4. read the response and edit the separately staged proposal;
5. choose **Apply to scene**, Copy, or Discard.

Agent output never edits the Plan while it is being generated. Apply is the only
action that replaces `shots[n].prompt`, and it uses the same synchronized Plan
write path as manual typing. If the scene changes after a request starts, the
draft is marked stale and **Apply anyway…** asks for confirmation. **Undo last
apply** restores the prompt that existed immediately before Apply.

Each editor uses a restricted prompt-only conversation separate from the
sidebar panel chat. Codex runs an ephemeral, read-only app-server thread with
MCP disabled; Hermes runs a prompt-only one-shot turn. Connecting an already
running console agent directly to the node is a separate follow-up integration,
not part of this first bridge.

`scene_range` on Loop Start is continuity-safe:

| Value | Result |
|---|---|
| blank | `start_clip` through the end |
| `3` | scene 3 only |
| `3:8` | scenes 3 through 8, inclusive |

A range starting after scene 1 requires its predecessor checkpoint. Disjoint
selections such as `1,3,5:8` are rejected because skipped scenes would break
the motion dependency.

## Review and resume

Place **Review Gate** between Segment + Checkpoint and Loop End. It plays the
current MP4 with synchronized audio and offers:

- **Approve & continue**
- **Retry prompt / seed**
- **Reroll seed**
- **Approve & stop**, optionally assembling a partial video

Notification sound, auto-continue timeout, and model unloading while waiting
are optional. The same node can preview and load **Resume scene N**; resume
validates the plan, audio hash, fingerprint, and predecessor artifacts first.
Drag the thin bar directly below the video to resize the preview; its height is
saved with the workflow. Double-click the bar to restore the default height.

While sampling is still in progress, a floating **Cancel & reroll scene N**
button is available. It targets only the active H3 prompt, waits for ComfyUI to
confirm interruption, writes a new explicit seed into that scene, and queues a
checkpoint resume from the same scene. It never falls back to ComfyUI's global
interrupt. Once Segment Save or Review Gate begins, the floating action hides
and Review Gate's normal reroll owns the retry.

## Archival PNG export

Connect a completed or partial manifest and the original H3 video VAE to
**Export PNG Sequence**. It loads each safetensors checkpoint independently,
decodes its full video latent, removes that scene's repeated overlap, and writes
one continuous `frame_00000001.png` sequence under
`output/h3_chains/<run_name>/frames/`. Scenes are released between decodes, so
the complete movie is never accumulated in RAM.

The export is lossless after conversion to standard 8-bit RGB PNG. Use the same
VAE and decode precision/settings to reproduce the original decode as closely
as possible. Existing export folders are never overwritten, and `export.json`
maps every frame range back to its checkpoint, prompt, and seed. The first PNG
can also carry the archived ComfyUI workflow and manifest.

## Audio modes

| Mode | Use it for |
|---|---|
| `source_track` | Music/video work. Wire the same full song to Loop Start, Current Shot, and Assemble. |
| `generated_audio` | No source track. Carry the previous compact AV latent and concatenate checkpointed audio. |
| `source_plus_timeline` | Experimental combination of source reference audio and generated timeline context. |

Core ComfyUI AUDIO dictionaries and compatible lazy/proxy AUDIO values are
supported for source tracks and scheduled audio references.

`source_track` is recommended for music video. The final MP4 still follows the
selected audio source, but H3's own decoded output is never hidden by that mux:
when audio is connected to Segment Save, each scene gets an uncompressed WAV in
`output/h3_chains/<run_name>/generated_audio/`, and a completed assembly also
writes `<final-name>.generated.wav` beside the final MP4. This leaves H3's
ambience, effects, and regenerated performance available for post-production.

Every segment also keeps its exact prompt in the MP4 metadata, a matching
`.prompt.txt`, its checkpoint JSON, and the safetensors metadata. The run stores
`plan.json`, loadable `workflow.json`, and `api_prompt.json`; review-gate prompt
or seed retries update these recovery copies before the replacement segment is
committed. Segment and assembled MP4s also use ComfyUI's standard embedded
`workflow` and `prompt` tags.

For voice specifically, choose `source_track` when a complete prerecorded
performance must remain exact in the final video. Choose `generated_audio` when
`@voice` is only a short identity/timbre reference and H3 should generate new
speech. The audio mode controls timeline/final audio; it does not activate or
deactivate scheduled audio-reference tags.

## Compatibility and guardrails

- In legacy LiteGraph rendering, this pack automatically works around
  [ComfyUI frontend issue #12443](https://github.com/Comfy-Org/ComfyUI_frontend/issues/12443)
  for every node on the current canvas. The standalone **Legacy Widget Width
  Fix** node is no longer required in H3 workflows, but remains compatible if
  present. Disable the embedded workaround under **Settings → MiniMax H3
  Contex Loop → Compatibility → Widget widths** if needed.
- Upstream H3 Motion Context and this pack share patch-ownership markers; the
  second compatible copy stands down.
- ComfyUI’s native **MiniMax H3 Add Guide** API is detected automatically. On
  that API, core owns arbitrary video/audio guides and payload merging; this
  pack keeps only a marker-gated Ref2VA target-alignment correction. Put an
  official Add Guide node after Loop Context to add scene-local anchors.
- Kijai’s SolAttn H3 Morton observer composes safely in either activation order.
- Ref2VA refs remain intact; unknown wrappers and changed layout assumptions
  fail loudly instead of producing a subtly broken join.
- KJ preview bridging is loop-local. Keep Spectrum/step-skipping disabled.

MiniMax H3 support is moving quickly. The pack checks the live ComfyUI layout
the first time Context runs; after updating ComfyUI or related H3 optimizers,
restart fully so wrapper ownership is rebuilt cleanly.

## More

- [Prompt, timing, audio, and resume format guide](H3_CHAIN_FORMAT_GUIDE.md)
- [Workflow notes](example_workflows/README.md)
- [Third-party notices and attribution](THIRD_PARTY_NOTICES.md)
- `tests/seam_probe.py` for measured audio-join analysis; `tests/` for the
  standalone node, patch, chain, and frontend checks

## License

GPL-3.0. See [LICENSE](LICENSE). Third-party inspiration and contributions are
recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
