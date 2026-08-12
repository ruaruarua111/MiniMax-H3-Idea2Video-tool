# Agent-backed prompt assistant study

## Finding

Yes, the Scene Prompt Editor can host an interactive prompt assistant for Codex
and Hermes.

The first implementation now exists on this study branch together with the
`feature/prompt-assistant-bridge` comfyui-mcp branch. It follows the isolated,
staged design below. Codex uses app-server; the initial Hermes adapter uses its
tool-restricted one-shot interface while a persistent ACP adapter remains a
possible later optimization.

The recommended architecture is:

```text
Scene Prompt Editor DOM widget
        |
        | prompt-assist request / progress / staged result
        v
comfyui-mcp local orchestrator
        |
        +-- Codex app-server backend
        +-- Hermes tool-restricted one-shot adapter (initial)
            +-- persistent ACP adapter (later option)
```

The assistant should have its own restricted conversation and a correlated
request/response protocol. It should not borrow the sidebar panel's main chat
session, open a second socket under the panel's workflow identity, or launch an
agent process from ComfyUI's Python server.

## What exists today

### This pack

`MiniMaxH3ChainScenePromptEditor` is a pass-through Python node. Its useful
behavior lives in `web/h3_chain_scene_prompt_editor.js` as a DOM widget.

The editor finds the upstream `MiniMaxH3ChainPlan`, parses its `plan_json`, and
writes the active scene's real `shots[n].prompt` back to that Plan widget on
every edit. There is no duplicate prompt value. This is a good base for an
assistant because a draft can be staged beside the textarea and applied through
the existing `writePlan()` path.

### comfyui-mcp and its panel

The panel already connects a browser client to a local `comfyui-mcp`
orchestrator over WebSocket. The orchestrator owns provider authentication,
agent sessions, streaming, model discovery, interruption, and live ComfyUI
tools. Codex is a native orchestrator backend.

Hermes support currently means `comfyui-mcp setup hermes`: it registers the
ComfyUI MCP server in Hermes so a Hermes console agent can control ComfyUI.
Hermes is not currently a panel-orchestrator backend and does not speak the
panel bridge protocol.

The panel bridge cannot safely be reused as-is by this node:

- its bridge client is private to the panel bundle;
- a workflow route is designed to have one live browser socket;
- the orchestrator deliberately shares one conversation per provider across
  panel tabs and workflows;
- `user_message` replies are chat frames, not request-correlated rewrite
  results.

A second node client using the current `hello`/`user_message` protocol could
replace the panel's route or mix rewrite output into the user's main panel
conversation.

### Agent-native integration surfaces

Codex documents app-server as the interface for a deep product integration. It
provides threads, turns, streamed events, approvals, interruption, and JSON-RPC.
`codex exec` is intended for non-interactive scripts and is useful for a small
one-shot proof of concept, but app-server (or the Codex SDK which controls it)
is the appropriate persistent implementation. `comfyui-mcp` already contains a
Codex app-server adapter, so that work should be reused.

The installed Hermes CLI exposes three relevant surfaces:

- `hermes --oneshot` for a scriptable one-shot response;
- `hermes --resume <session>` for continuing a stored session;
- `hermes acp` and `hermes serve` for editor/backend integrations.

ACP or the backend service is preferable for the final persistent assistant.
One-shot mode is sufficient to validate prompt quality and UX before adding a
full Hermes backend adapter.

## Recommended user experience

Add a collapsible **Prompt Assistant** area below the scene textarea:

- provider: Codex or Hermes;
- action presets: Rewrite, Improve continuity, Shorten, Critique;
- a free-form instruction box and Send/Stop buttons;
- a compact transcript for follow-up requests;
- a staged draft view with Apply, Copy, and Discard;
- connection, working, and error status that does not replace the editor's
  existing Plan synchronization status.

The agent's result must be staged. Streaming tokens must never mutate the Plan.
Only an explicit Apply/Replace action should call the existing `writePlan()`
path.

Suggested interaction:

1. The user edits the current scene prompt.
2. The user asks, for example, "Make the camera movement more precise and keep
   the unfinished action for scene 4."
3. The node sends the current scene, shared prompt, and bounded neighboring
   continuity context.
4. The UI shows working/progress state while the assistant builds a structured
   result.
5. The node shows the proposed prompt separately.
6. The user applies or discards it, then can continue chatting about the staged
   draft.

The entire plan should not be sent by default. Useful bounded context is:

- shared prompt;
- current scene id, duration, and prompt;
- previous scene's ending and next scene's opening (or the complete neighboring
  prompts while they remain under a strict size limit);
- reference tags available to the workflow;
- the prompt-writing rules bundled with this pack.

## Implemented bridge protocol

The implementation adds a prompt-assistant client kind and correlated frames to
`comfyui-mcp`.

Client request:

```json
{
  "type": "prompt_assist_request",
  "request_id": "pa-uuid",
  "conversation_id": "h3-browser-instance-uuid",
  "provider": "codex",
  "mode": "rewrite",
  "instruction": "Improve camera precision and ending continuity.",
  "source_revision": "opaque-source-text-and-scene-id-revision",
  "context": {
    "scene_id": "clip_0003",
    "source_prompt": "...",
    "selected_text": null,
    "shared_prompt": "...",
    "previous_prompt": "...",
    "next_prompt": "..."
  }
}
```

Server frames:

```json
{"type":"prompt_assist_started","request_id":"pa-uuid","provider":"codex"}
{"type":"prompt_assist_progress","request_id":"pa-uuid"}
{
  "type": "prompt_assist_result",
  "request_id": "pa-uuid",
  "source_revision": "opaque-source-text-and-scene-id-revision",
  "message": "What changed and why.",
  "rewritten_prompt": "The complete proposed scene prompt."
}
{"type":"prompt_assist_error","request_id":"pa-uuid","error":"..."}
```

Cancellation:

```json
{"type":"prompt_assist_cancel","request_id":"pa-uuid"}
```

The canonical final result is schema-constrained. A conversational message and
the complete replacement prompt are separate fields. Codex enforces the schema;
the initial Hermes adapter also accepts a plain-text fallback so older local
Hermes models remain usable.

For an HTTPS-hosted ComfyUI, use the panel's existing advertised secure bridge
mechanism. A page served over HTTPS cannot directly use an insecure localhost
WebSocket.

## Session and tool boundaries

Use a session key such as:

```text
prompt-assistant::<client-id>::<node-id>::<provider>
```

This session must be separate from `orchestrator::<provider>`, the panel's main
conversation. Switching providers should preserve each provider's assistant
session, as the panel already does for its provider conversations.

The prompt assistant should be text-only and tool-free by default:

- no shell or file editing;
- no graph mutation tools;
- no workflow execution;
- no web access unless the user explicitly enables it later;
- read-only, minimal sandbox for Codex;
- bounded prompt, response, turn duration, and queued request count.

This keeps "rewrite my prompt" from becoming an implicit authorization for an
agent to edit the workspace or run the graph. If graph-aware tools are wanted
later, they should be a separate explicit mode.

The current console session should not be scraped through a pseudo-terminal or
shared concurrently. A console and the node may resume the same stored session
by id at different times, but the normal node experience should use a dedicated
assistant session. The UI may expose Copy session id / Reset conversation for
advanced use.

## Edit-safety rules

Every request records the active scene id and a revision hash of the source
text. When a result arrives:

- if both still match, enable Apply;
- if the user changed scenes or edited the source, keep the result staged but
  show "Source changed while the assistant was working";
- never apply a late result to whichever scene happens to be active;
- keep the user's current text available as a one-step local undo after Apply;
- discard or cancel outstanding requests when the node is removed;
- cap transcript persistence and do not serialize auth data or bridge tokens in
  the workflow.

Prompt text is untrusted data. Put it in a clearly delimited data field and keep
the rewrite instruction outside that field. The structured result schema is the
authority for what may be applied.

## Implementation phases

### Phase 0: one-shot UX proof (superseded)

The original proposal was a manually started local development sidecar outside
ComfyUI's Python process. The native bridge was implemented directly instead,
so no separate sidecar is required.

- Codex: `codex exec --ephemeral --sandbox read-only` with structured output.
- Hermes: `hermes --oneshot` with tools disabled and a structured-output
  contract where supported.
- Node: provider picker, instruction, staged result, conflict detection, Apply.

This proves prompt quality and editor ergonomics. It should be marked a local
development mode, not the shipping architecture.

### Phase 1: native comfyui-mcp capability (implemented for Codex and initial Hermes)

- add the prompt-assist frame family and dedicated session manager;
- reuse the current Codex backend/app-server lifecycle;
- use Hermes' tool-restricted one-shot adapter initially; ACP remains the
  persistent-session optimization;
- expose provider readiness specifically for prompt assistance;
- support progress state, cancellation, reset, and structured final output;
- keep the existing panel protocol and shared panel sessions unchanged.

### Phase 2: production editor UI (initial implementation complete)

- implement the assistant panel in `web/h3_chain_scene_prompt_editor.js`;
- factor transport and revision helpers into testable `.mjs` modules;
- keep a transient node transcript plus bounded orchestrator transcript state;
  neither is serialized into the workflow;
- include selected text as special context and provide an editable staged
  replacement with original/proposal comparison;
- document startup and unavailable-provider states.

## Test plan

Current frontend tests cover bounded context construction, source revisions,
stale-draft detection, bridge discovery, deferred conversation reset, Plan
round-trips, and the editor's staged controls. Current orchestrator tests cover
request validation, output parsing, correlated result/cancellation, transcript
reset and provider separation, Codex isolation settings, auxiliary socket
routing, and existing conversation-boundary behavior.

A later browser-level automation pass should additionally cover:

- request context for first, middle, and last scenes;
- multiline prompt round trips;
- request/result correlation with out-of-order replies;
- navigation and source edits while a request is in flight;
- Apply, Discard, and one-step undo through real DOM events;
- cancellation and node removal;
- bridge loss/reconnect without duplicate Apply;
- no bridge token, credential, or transcript leakage into `plan_json`.

Orchestrator tests should cover:

- assistant sessions never resolve to the panel's shared session key;
- no panel/graph tools are registered for assistant turns;
- provider switch and per-provider resume;
- schema validation and size/time limits;
- cancellation before and during generation;
- Codex and Hermes adapters produce the same canonical result frames;
- a malicious prompt cannot change the requested response shape or tool policy.

A browser smoke test should edit a real Plan prompt, request a rewrite, verify
that agent progress does not change `plan_json`, Apply the draft, and confirm
both the large editor and the Plan editor show the same saved text.

## Decision

Proceed with Phase 0 only as a short-lived UX experiment. The production target
should be Phase 1 plus Phase 2: a provider-neutral prompt-assist capability in
`comfyui-mcp` and a staged assistant UI in this editor.

This gives Codex and Hermes one consistent node experience, keeps user
authentication and agent processes out of ComfyUI, preserves Registry-friendly
separation, avoids interference with the sidebar agent, and leaves room for the
same assistant transport to serve other prompt-editor nodes later.

## Evidence reviewed

- this pack's `chain_nodes.py`, `web/h3_chain_scene_prompt_editor.js`, and
  `web/h3_chain_plan_core.mjs`;
- `/media/p5/comfyui-mcp/src/orchestrator/agent-backend.ts`, `index.ts`,
  `codex-backend.ts`, and `src/services/agent-setup.ts`;
- `/media/p5/comfyui-mcp-panel/__init__.py` and
  `web/js/comfyui-mcp-panel.js`;
- the locally installed `codex exec`, `hermes`, `hermes acp`, and
  `hermes serve` command contracts;
- OpenAI's current [Codex App Server](https://learn.chatgpt.com/docs/app-server.md),
  [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk.md), and
  [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode.md)
  documentation.
