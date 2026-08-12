import {app} from "/scripts/app.js";
import {api} from "/scripts/api.js";
import {
    parsePlanJson,
    planToJson,
    promptTextToLines,
    promptValueToText,
    sharedPrompt,
} from "./h3_chain_plan_core.mjs";
import {
    PROMPT_ASSIST_DEFAULT_INSTRUCTIONS,
    PROMPT_ASSIST_MODES,
    buildPromptAssistantContext,
    draftConflict,
    makePromptAssistRequest,
    promptSceneKey,
    promptSourceRevision,
} from "./h3_prompt_assistant_core.mjs";
import {PromptAssistantClient} from "./h3_prompt_assistant_client.mjs";
import {availableReferenceRecords} from "./h3_reference_preview_core.mjs";

// The compact @ reference and # dialogue authoring interactions are inspired
// by nkxx188/ComfyUI-MiniMaxH3-Easy (MIT); see THIRD_PARTY_NOTICES.md.

const NODE_NAME = "MiniMaxH3ChainScenePromptEditor";
const PLAN_NAME = "MiniMaxH3ChainPlan";
const ACTIVE_SCENE_PROPERTY = "h3_scene_prompt_editor_active_scene";
const FONT_SIZE_PROPERTY = "h3_scene_prompt_editor_font_size";
const ASSIST_PROVIDER_PROPERTY = "h3_scene_prompt_editor_assist_provider";
const ASSIST_MODE_PROPERTY = "h3_scene_prompt_editor_assist_mode";
// Keep the complete prompt-assistant implementation available for a future
// revisit, but ship the original focused editor experience for now. Re-enabling
// it is intentionally a one-line change.
const PROMPT_ASSISTANT_ENABLED = false;
const DEFAULT_FONT_SIZE = 18;
const MIN_FONT_SIZE = 12;
const MAX_FONT_SIZE = 36;

function injectStyles() {
    if (document.getElementById("h3-scene-prompt-editor-style")) return;
    const style = document.createElement("style");
    style.id = "h3-scene-prompt-editor-style";
    style.textContent = `
        .h3sp-root {
            --h3sp-bg: color-mix(in srgb, var(--comfy-menu-bg, #202124) 92%, #101827);
            --h3sp-panel: color-mix(in srgb, var(--comfy-input-bg, #111827) 84%, #263552);
            --h3sp-border: color-mix(in srgb, var(--border-color, #555) 68%, #7891bf);
            --h3sp-text: var(--input-text, #eef1f7);
            --h3sp-muted: color-mix(in srgb, var(--h3sp-text) 58%, transparent);
            --h3sp-accent: #84aaff;
            --h3sp-font-size: 18px;
            box-sizing:border-box; width:100%; height:100%; min-height:420px;
            display:flex; flex-direction:column; gap:8px; overflow:hidden; padding:10px;
            border:1px solid var(--h3sp-border); border-radius:8px; background:var(--h3sp-bg);
            color:var(--h3sp-text); font:12px/1.35 system-ui,sans-serif;
        }
        .h3sp-root *, .h3sp-root *::before, .h3sp-root *::after { box-sizing:border-box; }
        .h3sp-head, .h3sp-nav, .h3sp-tools, .h3sp-font, .h3sp-footer {
            display:flex; align-items:center; gap:6px;
        }
        .h3sp-head { justify-content:space-between; }
        .h3sp-title { color:var(--h3sp-accent); font-size:15px; font-weight:750; }
        .h3sp-context { color:var(--h3sp-muted); white-space:nowrap; overflow:hidden;
            text-overflow:ellipsis; text-align:right; }
        .h3sp-nav select { flex:1; min-width:0; }
        .h3sp-root button, .h3sp-root select {
            color:var(--h3sp-text); font:inherit; border:1px solid var(--h3sp-border);
            border-radius:5px; background:var(--comfy-input-bg,#171a21);
        }
        .h3sp-root button { padding:6px 9px; cursor:pointer; white-space:nowrap; }
        .h3sp-root button:hover { border-color:var(--h3sp-accent); }
        .h3sp-root button:disabled { cursor:not-allowed; opacity:.4; }
        .h3sp-root select { min-height:30px; padding:5px 7px; }
        .h3sp-font { margin-left:auto; }
        .h3sp-font-value { min-width:38px; color:var(--h3sp-muted); text-align:center; }
        .h3sp-textarea {
            width:100%; min-height:240px; flex:1 1 auto; resize:none; padding:12px 14px;
            border:1px solid var(--h3sp-border); border-radius:7px;
            outline:none; background:var(--comfy-input-bg,#11141a); color:var(--h3sp-text);
            font:var(--h3sp-font-size)/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;
            tab-size:4; white-space:pre-wrap;
        }
        .h3sp-textarea:focus { border-color:var(--h3sp-accent);
            box-shadow:0 0 0 1px color-mix(in srgb,var(--h3sp-accent) 45%,transparent); }
        .h3sp-root.h3sp-assistant-enabled { overflow:auto; }
        .h3sp-root.h3sp-assistant-enabled .h3sp-textarea {
            min-height:220px; resize:vertical;
        }
        .h3sp-tools { position:relative; flex-wrap:wrap; }
        .h3sp-hint { color:var(--h3sp-muted); margin-left:auto; }
        .h3sp-refs { display:none; flex:0 0 auto; max-height:340px; overflow:auto;
            padding:7px; gap:5px; flex-wrap:wrap; border:1px solid var(--h3sp-border);
            border-radius:6px; background:var(--h3sp-panel); }
        .h3sp-refs.h3sp-open { display:flex; }
        .h3sp-refs button { padding:4px 7px; }
        .h3sp-ref-help { flex:1 0 100%; color:var(--h3sp-muted); }
        .h3sp-ref-chip.h3sp-inactive { opacity:.48; }
        .h3sp-ref-chip.h3sp-active { border-color:#658b77; }
        .h3sp-ref-preview { display:none; flex:1 0 100%; min-height:58px;
            padding:8px; border:1px solid color-mix(in srgb,var(--h3sp-border) 74%,transparent);
            border-radius:6px; background:var(--comfy-input-bg,#11141a); }
        .h3sp-ref-preview.h3sp-visible { display:grid; grid-template-columns:minmax(120px,220px) 1fr;
            align-items:start; gap:9px; }
        .h3sp-ref-preview-media { width:100%; max-height:190px; display:block;
            border-radius:5px; object-fit:contain; background:#08090c; }
        audio.h3sp-ref-preview-media { min-width:190px; height:38px; background:transparent; }
        .h3sp-ref-preview-copy { min-width:0; color:var(--h3sp-muted);
            white-space:pre-wrap; overflow-wrap:anywhere; }
        .h3sp-ref-preview-title { color:var(--h3sp-text); font-weight:700; }
        .h3sp-footer { justify-content:space-between; color:var(--h3sp-muted); }
        .h3sp-error { padding:12px; border:1px solid #a76565; border-radius:6px;
            color:#ffb3b3; background:#351f24; white-space:pre-wrap; }
        .h3sp-assist { flex:0 0 auto; display:flex; flex-direction:column; gap:7px;
            padding:9px; border:1px solid var(--h3sp-border); border-radius:7px;
            background:var(--h3sp-panel); }
        .h3sp-assist-head, .h3sp-assist-controls, .h3sp-assist-actions,
        .h3sp-assist-contexts { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
        .h3sp-assist-head { justify-content:space-between; }
        .h3sp-assist-title { color:var(--h3sp-accent); font-size:13px; font-weight:750; }
        .h3sp-assist-status { color:var(--h3sp-muted); font-size:11px; }
        .h3sp-assist-controls select { flex:1 1 120px; min-width:100px; }
        .h3sp-assist-contexts label { display:flex; align-items:center; gap:4px;
            color:var(--h3sp-muted); cursor:pointer; }
        .h3sp-assist-contexts input { accent-color:var(--h3sp-accent); }
        .h3sp-assist-chat { display:flex; flex-direction:column; gap:5px; max-height:180px;
            overflow:auto; padding:6px; border:1px solid color-mix(in srgb,var(--h3sp-border) 70%,transparent);
            border-radius:6px; background:color-mix(in srgb,var(--comfy-input-bg,#11141a) 82%,transparent); }
        .h3sp-assist-message { padding:6px 8px; border-radius:6px; white-space:pre-wrap;
            overflow-wrap:anywhere; }
        .h3sp-assist-message-user { margin-left:12%; background:#23375b; }
        .h3sp-assist-message-agent { margin-right:7%; background:#263c34; }
        .h3sp-assist-message-system { color:var(--h3sp-muted); font-size:11px;
            border:1px dashed var(--h3sp-border); }
        .h3sp-assist-empty { color:var(--h3sp-muted); padding:5px; }
        .h3sp-assist-compose { display:flex; align-items:stretch; gap:6px; }
        .h3sp-assist-compose textarea, .h3sp-assist-draft textarea {
            width:100%; resize:vertical; min-height:58px; padding:7px 8px;
            border:1px solid var(--h3sp-border); border-radius:6px;
            outline:none; background:var(--comfy-input-bg,#11141a); color:var(--h3sp-text);
            font:12px/1.4 system-ui,sans-serif; }
        .h3sp-assist-compose textarea:focus, .h3sp-assist-draft textarea:focus {
            border-color:var(--h3sp-accent); }
        .h3sp-assist-compose button { align-self:stretch; }
        .h3sp-assist-draft { display:flex; flex-direction:column; gap:6px; padding:8px;
            border:1px solid #5d8a72; border-radius:6px; background:#182b25; }
        .h3sp-assist-draft-head { display:flex; justify-content:space-between;
            align-items:center; gap:6px; }
        .h3sp-assist-draft-title { font-weight:700; color:#9ad7b7; }
        .h3sp-assist-stale { color:#ffc08a; font-size:11px; }
        .h3sp-assist-original { color:var(--h3sp-muted); }
        .h3sp-assist-original pre { max-height:120px; overflow:auto; white-space:pre-wrap;
            padding:6px; border-radius:5px; background:var(--comfy-input-bg,#11141a); }
        .h3sp-assist-error { color:#ffb3b3; white-space:pre-wrap; }
    `;
    document.head.appendChild(style);
}

function element(tag, className = "", text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
}

function button(label, title, action) {
    const item = element("button", "", label);
    item.type = "button";
    item.title = title;
    item.addEventListener("click", action);
    return item;
}

function nodeType(node) {
    return node?.comfyClass ?? node?.type ?? null;
}

function allNodes(graph, output = []) {
    for (const node of graph?._nodes ?? []) {
        output.push(node);
        if (node.subgraph) allNodes(node.subgraph, output);
    }
    return output;
}

function upstreamPlanNode(start) {
    const queue = [start];
    const seen = new Set();
    while (queue.length) {
        const node = queue.shift();
        if (!node || seen.has(node)) continue;
        seen.add(node);
        if (node !== start && nodeType(node) === PLAN_NAME) return node;
        for (const input of node.inputs ?? []) {
            if (input.link == null) continue;
            const link = node.graph?.links?.[input.link];
            const parent = link ? node.graph?.getNodeById?.(link.origin_id) : null;
            if (parent) queue.push(parent);
        }
    }
    return null;
}

function inputSource(node, name) {
    const input = node?.inputs?.find((item) => item.name === name);
    const link = input?.link == null ? null : node.graph?.links?.[input.link];
    return link ? node.graph?.getNodeById?.(link.origin_id) ?? null : null;
}

function mediaExtension(kind) {
    if (kind === "image") return /\.(?:avif|bmp|gif|jpe?g|png|webp)$/i;
    if (kind === "video") return /\.(?:m4v|mkv|mov|mp4|webm)$/i;
    return /\.(?:aac|flac|m4a|mp3|ogg|opus|wav)$/i;
}

function widgetAsset(value, kind) {
    if (value && typeof value === "object" && value.filename) {
        return {
            filename: String(value.filename),
            subfolder: String(value.subfolder ?? ""),
            type: String(value.type ?? "input"),
        };
    }
    let text = typeof value === "string" ? value.trim() : "";
    if (!text) return null;
    if (/^(?:blob:|data:|https?:|\/api\/view\?|\/view\?)/i.test(text)) {
        return {url: text};
    }
    let type = "input";
    const annotated = text.match(/\s+\[(input|output|temp)\]\s*$/i);
    if (annotated) {
        type = annotated[1].toLowerCase();
        text = text.slice(0, annotated.index).trim();
    }
    text = text.replaceAll("\\", "/").replace(/^\/+/, "");
    if (!mediaExtension(kind).test(text)) return null;
    const slash = text.lastIndexOf("/");
    return {
        filename: slash >= 0 ? text.slice(slash + 1) : text,
        subfolder: slash >= 0 ? text.slice(0, slash) : "",
        type,
    };
}

function assetUrl(asset) {
    if (!asset) return null;
    if (asset.url) return asset.url;
    const query = new URLSearchParams({
        filename: asset.filename,
        subfolder: asset.subfolder ?? "",
        type: asset.type ?? "input",
    });
    return api.apiURL(`/view?${query.toString()}`);
}

function previewFromNode(node, kind) {
    if (kind === "image") {
        const rendered = node?.imgs?.[0];
        const src = typeof rendered === "string" ? rendered : rendered?.src;
        if (src) return src;
    }
    for (const widget of node?.widgets ?? []) {
        const asset = widgetAsset(widget.value, kind);
        if (asset) return assetUrl(asset);
    }
    return null;
}

function findMediaPreview(start, kind) {
    const queue = [start];
    const seen = new Set();
    while (queue.length) {
        const node = queue.shift();
        if (!node || seen.has(node)) continue;
        seen.add(node);
        const url = previewFromNode(node, kind);
        if (url) return {url, source: node};
        for (const input of node.inputs ?? []) {
            const parent = inputSource(node, input.name);
            if (parent) queue.push(parent);
        }
    }
    return {url: null, source: start};
}

function insertText(textarea, text, selectionOffset = text.length) {
    const start = textarea.selectionStart ?? textarea.value.length;
    const end = textarea.selectionEnd ?? start;
    textarea.setRangeText(text, start, end, "end");
    const caret = start + selectionOffset;
    textarea.setSelectionRange(caret, caret);
    textarea.dispatchEvent(new Event("input", {bubbles: true}));
    textarea.focus();
}

function insertDialogue(textarea) {
    const start = textarea.selectionStart ?? textarea.value.length;
    const end = textarea.selectionEnd ?? start;
    const selected = textarea.value.slice(start, end);
    const markup = `<d>${selected}</d>`;
    insertText(textarea, markup, selected ? markup.length : 3);
}

function clamp(value, minimum, maximum, fallback) {
    const numeric = Number(value);
    return Number.isFinite(numeric)
        ? Math.max(minimum, Math.min(maximum, Math.round(numeric))) : fallback;
}

function promptAssistantIdentityKey(node) {
    // Node ids are only unique inside one workflow. ComfyUI can keep several
    // workflows open in the same browser/sessionStorage, so include the active
    // workflow identity or two editors with (for example) node id 7 would share
    // one agent route and pending-request record.
    const workflow = app.extensionManager?.workflow?.activeWorkflow;
    const workflowIdentity = workflow?.path
        ?? workflow?.activeState?.id
        ?? workflow?.filename
        ?? "legacy-workflow";
    const nodeIdentity = node.id == null
        ? `new-${globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2)}`
        : String(node.id);
    return `workflow-${workflowIdentity}-node-${nodeIdentity}`;
}

function mount(node) {
    if (node._h3ScenePromptEditorMounted || typeof node.addDOMWidget !== "function") return;
    node._h3ScenePromptEditorMounted = true;
    injectStyles();

    node.properties ??= {};
    const root = element("div", "h3sp-root");
    root.classList.toggle("h3sp-assistant-enabled", PROMPT_ASSISTANT_ENABLED);
    root.title = "Edit the active scene prompt stored in the connected H3 Chain Plan.";
    for (const eventName of [
        "pointerdown", "pointerup", "mousedown", "mouseup", "click", "dblclick",
    ]) {
        root.addEventListener(eventName, (event) => event.stopPropagation());
    }
    root.addEventListener("wheel", (event) => event.stopPropagation());

    const state = {
        plan: null,
        planNode: null,
        planWidget: null,
        lastValue: "",
        active: Math.max(0, Number(node.properties[ACTIVE_SCENE_PROPERTY]) || 0),
        fontSize: clamp(
            node.properties[FONT_SIZE_PROPERTY], MIN_FONT_SIZE, MAX_FONT_SIZE,
            DEFAULT_FONT_SIZE,
        ),
        assistant: {
            client: null,
            host: null,
            status: "idle",
            statusDetail: "Connects through comfyui-mcp on the first request",
            provider: ["codex", "hermes"].includes(
                node.properties[ASSIST_PROVIDER_PROPERTY],
            ) ? node.properties[ASSIST_PROVIDER_PROPERTY] : "codex",
            mode: PROMPT_ASSIST_MODES.some(
                (item) => item.id === node.properties[ASSIST_MODE_PROPERTY],
            ) ? node.properties[ASSIST_MODE_PROPERTY] : "rewrite",
            includeShared: true,
            includeAdjacent: true,
            composer: "",
            messagesByProvider: {codex: [], hermes: []},
            drafts: new Map(),
            requestContexts: new Map(),
            activeRequest: null,
            preparingRequest: null,
            pendingStorageKey: "",
            reconnectTimer: null,
            lastApplied: null,
            providers: null,
            error: "",
        },
        pollTimer: null,
    };
    node._h3ScenePromptEditorState = state;

    const assistant = state.assistant;
    if (PROMPT_ASSISTANT_ENABLED) {
        assistant.client = new PromptAssistantClient({
            identityKey: promptAssistantIdentityKey(node),
            onFrame: (frame) => handleAssistantFrame(frame),
            onStatus: (status, detail) => {
                assistant.status = status;
                if (status === "connected") {
                    clearAssistantReconnect();
                    assistant.statusDetail = "Connected · isolated prompt session";
                } else if (status === "connecting") {
                    assistant.statusDetail = "Connecting to comfyui-mcp…";
                } else if (assistant.activeRequest) {
                    assistant.statusDetail = "Disconnected · reconnecting to recover the active draft…";
                    scheduleAssistantReconnect();
                } else {
                    assistant.statusDetail = "Disconnected · send to reconnect";
                }
                if (detail?.providers) assistant.providers = detail.providers;
                refreshAssistant();
            },
        });
        assistant.pendingStorageKey = `h3.prompt-assistant.pending.${assistant.client.identity}`;
    }

    function dirty() {
        node.graph?.setDirtyCanvas?.(true, true);
        app.graph?.setDirtyCanvas?.(true, true);
    }

    function persistView() {
        node.properties[ACTIVE_SCENE_PROPERTY] = state.active;
        node.properties[FONT_SIZE_PROPERTY] = state.fontSize;
        dirty();
    }

    function writePlan(status) {
        if (!state.plan || !state.planWidget || !state.planNode) return;
        const value = planToJson(state.plan);
        state.lastValue = value;
        state.planWidget.value = value;
        state.planWidget.callback?.(value);
        state.planNode._h3ChainEditorRefresh?.();
        state.planNode.graph?.setDirtyCanvas?.(true, true);
        if (status) status.textContent = "Saved to connected Plan";
        dirty();
    }

    function messagesForProvider(provider = assistant.provider) {
        const key = provider === "hermes" ? "hermes" : "codex";
        assistant.messagesByProvider[key] ??= [];
        return assistant.messagesByProvider[key];
    }

    function assistantMessage(role, text, provider = assistant.provider) {
        const value = String(text ?? "").trim();
        if (!value) return;
        const messages = messagesForProvider(provider);
        messages.push({role, text: value});
        assistant.messagesByProvider[provider === "hermes" ? "hermes" : "codex"] = messages.slice(-24);
    }

    function clearAssistantReconnect() {
        if (assistant.reconnectTimer != null) {
            window.clearTimeout(assistant.reconnectTimer);
            assistant.reconnectTimer = null;
        }
    }

    function scheduleAssistantReconnect(delay = 500) {
        if (!assistant.activeRequest || assistant.reconnectTimer != null) return;
        assistant.reconnectTimer = window.setTimeout(async () => {
            assistant.reconnectTimer = null;
            if (!assistant.activeRequest) return;
            try {
                await assistant.client.connect();
            } catch (_error) {
                if (assistant.activeRequest) scheduleAssistantReconnect(1_000);
            }
        }, delay);
    }

    function persistPendingRequest(requestId, meta) {
        try {
            globalThis.sessionStorage?.setItem(assistant.pendingStorageKey, JSON.stringify({
                requestId,
                ...meta,
            }));
        } catch (_error) {
            // Recovery across reload is best-effort; live correlation still works.
        }
    }

    function clearPendingRequest(requestId = null) {
        if (requestId && assistant.activeRequest && assistant.activeRequest !== requestId) return;
        try { globalThis.sessionStorage?.removeItem(assistant.pendingStorageKey); } catch (_error) { /* unavailable */ }
    }

    function restorePendingRequest() {
        let stored = null;
        try {
            stored = JSON.parse(globalThis.sessionStorage?.getItem(assistant.pendingStorageKey) || "null");
        } catch (_error) {
            clearPendingRequest();
        }
        if (!stored || typeof stored !== "object" || typeof stored.requestId !== "string") return;
        const meta = {
            sceneKey: String(stored.sceneKey || ""),
            sceneId: String(stored.sceneId || ""),
            sceneIndex: Number(stored.sceneIndex),
            sourcePrompt: String(stored.sourcePrompt ?? ""),
            sourceRevision: String(stored.sourceRevision || ""),
            provider: stored.provider === "hermes" ? "hermes" : "codex",
        };
        if (!meta.sceneKey || !meta.sceneId || !Number.isInteger(meta.sceneIndex) || !meta.sourceRevision) {
            clearPendingRequest();
            return;
        }
        assistant.provider = meta.provider;
        assistant.activeRequest = stored.requestId;
        assistant.requestContexts.set(stored.requestId, meta);
        assistant.status = "connecting";
        assistant.statusDetail = "Reconnecting to recover the active draft…";
        refreshAssistant();
        scheduleAssistantReconnect(0);
    }

    function reconcileAssistantProvider() {
        // A restored in-flight request remains owned by its original provider.
        // Switching the visible lane during recovery would hide its reply in a
        // different provider transcript.
        if (assistant.activeRequest) return;
        const selected = assistant.providers?.find((item) => item.id === assistant.provider);
        if (selected?.available !== false) return;
        const fallback = assistant.providers?.find((item) => item.available !== false);
        if (!fallback || !["codex", "hermes"].includes(fallback.id)) return;
        const unavailable = assistant.provider === "hermes" ? "Hermes" : "Codex";
        assistant.provider = fallback.id;
        node.properties[ASSIST_PROVIDER_PROPERTY] = assistant.provider;
        assistantMessage("system", `${unavailable} is unavailable; using ${fallback.label || fallback.id}.`, assistant.provider);
        dirty();
    }

    function assistantProviderAvailable(provider = assistant.provider) {
        return assistant.providers?.find((item) => item.id === provider)?.available !== false;
    }

    function refreshAssistant() {
        const host = assistant.host;
        const promptTextarea = root.querySelector(".h3sp-textarea");
        if (!host || !promptTextarea || !state.plan?.shots?.length) return;
        renderAssistant(host, promptTextarea);
    }

    function handleAssistantFrame(frame) {
        if (!frame || typeof frame !== "object") return;
        if (frame.type === "prompt_assist_ready") {
            assistant.providers = Array.isArray(frame.providers) ? frame.providers : null;
            reconcileAssistantProvider();
            assistant.error = "";
        } else if (frame.type === "prompt_assist_started") {
            if (frame.request_id === assistant.activeRequest) assistant.status = "working";
        } else if (frame.type === "prompt_assist_progress") {
            if (frame.request_id === assistant.activeRequest) assistant.statusDetail = "Agent is drafting…";
        } else if (frame.type === "prompt_assist_result") {
            // Accept only a request this editor incarnation has in its live or
            // sessionStorage-restored correlation map. A disconnected "New
            // chat" can reconnect to a buffered result before its reset frame
            // reaches the server; reconstructing metadata from that result
            // would resurrect a draft the user explicitly discarded.
            const meta = assistant.requestContexts.get(frame.request_id);
            if (!meta) return;
            assistant.requestContexts.delete(frame.request_id);
            if (assistant.activeRequest === frame.request_id) assistant.activeRequest = null;
            clearPendingRequest(frame.request_id);
            clearAssistantReconnect();
            assistant.status = "connected";
            assistant.statusDetail = "Connected · isolated prompt session";
            assistant.error = "";
            assistantMessage("agent", frame.message || "Draft ready.", meta.provider);
            if (typeof frame.rewritten_prompt === "string" && frame.rewritten_prompt.trim()) {
                assistant.drafts.set(meta.sceneKey, {
                    sceneId: meta.sceneId,
                    sceneIndex: meta.sceneIndex,
                    sourcePrompt: meta.sourcePrompt,
                    sourceRevision: meta.sourceRevision,
                    proposed: frame.rewritten_prompt,
                    provider: frame.provider || meta.provider,
                });
            } else if (typeof frame.rewritten_prompt === "string") {
                assistant.error = "The agent returned an empty draft, so it was not staged.";
            }
        } else if (frame.type === "prompt_assist_error") {
            const meta = assistant.requestContexts.get(frame.request_id);
            if (frame.request_id && !meta && assistant.activeRequest !== frame.request_id) return;
            if (meta) assistant.requestContexts.delete(frame.request_id);
            if (!frame.request_id || assistant.activeRequest === frame.request_id) {
                assistant.activeRequest = null;
            }
            clearPendingRequest(frame.request_id);
            clearAssistantReconnect();
            assistant.status = assistant.client?.socket?.readyState === WebSocket.OPEN
                ? "connected" : "disconnected";
            assistant.error = String(frame.error || "Prompt assistant failed.");
            assistantMessage("system", `Agent error: ${assistant.error}`, meta?.provider);
        } else if (frame.type === "prompt_assist_cancelled") {
            const meta = assistant.requestContexts.get(frame.request_id);
            if (!meta && assistant.activeRequest !== frame.request_id) return;
            assistant.requestContexts.delete(frame.request_id);
            if (assistant.activeRequest === frame.request_id) assistant.activeRequest = null;
            clearPendingRequest(frame.request_id);
            clearAssistantReconnect();
            assistant.status = "connected";
            assistant.statusDetail = "Stopped · ready for another request";
            assistantMessage("system", "Request stopped. The scene prompt was not changed.", meta?.provider);
        } else if (
            frame.type === "prompt_assist_cancel_ack"
            && frame.cancelled === false
            && frame.request_id === assistant.activeRequest
        ) {
            // The orchestrator may have restarted while the browser retained a
            // pending request. A negative correlated ack proves there is no
            // server-side turn left to wait for.
            const meta = assistant.requestContexts.get(frame.request_id);
            assistant.requestContexts.delete(frame.request_id);
            assistant.activeRequest = null;
            clearPendingRequest(frame.request_id);
            clearAssistantReconnect();
            assistant.status = assistant.client?.socket?.readyState === WebSocket.OPEN
                ? "connected" : "disconnected";
            assistant.statusDetail = "No active server request · ready for another request";
            assistant.error = "The prior request is no longer active, likely because the bridge restarted.";
            assistantMessage("system", assistant.error, meta?.provider);
        }
        refreshAssistant();
    }

    async function sendAssistant(promptTextarea) {
        if (assistant.activeRequest || assistant.preparingRequest || !state.plan?.shots?.length) return;
        // Snapshot the selected scene before the asynchronous bridge handshake.
        // Navigation remains usable while connecting; reading state.active and
        // an old textarea after await could otherwise pair scene B's id with
        // scene A's prompt.
        const requestSceneIndex = state.active;
        const requestShot = state.plan.shots[requestSceneIndex];
        const sourcePrompt = promptTextarea.value;
        const selectedText = sourcePrompt.slice(
            promptTextarea.selectionStart ?? 0,
            promptTextarea.selectionEnd ?? 0,
        );
        const context = buildPromptAssistantContext(
            state.plan,
            requestSceneIndex,
            sourcePrompt,
            {
                includeShared: assistant.includeShared,
                includeAdjacent: assistant.includeAdjacent,
                selectedText,
            },
        );
        const preparation = {};
        assistant.preparingRequest = preparation;
        assistant.status = "connecting";
        assistant.statusDetail = "Connecting to comfyui-mcp…";
        assistant.error = "";
        refreshAssistant();
        try {
            // Wait for prompt_assist_ready before freezing the provider into the
            // request. The ready frame may switch a persisted unavailable Hermes
            // selection to Codex.
            await assistant.client.connect();
        } catch (error) {
            // New chat may have invalidated this connect attempt while it was
            // awaiting the handshake. In that case it owns no UI state.
            if (assistant.preparingRequest !== preparation) return;
            assistant.preparingRequest = null;
            assistant.status = "disconnected";
            assistant.error = error.message || String(error);
            assistantMessage("system", `Could not connect: ${assistant.error}`);
            refreshAssistant();
            return;
        }
        if (assistant.preparingRequest !== preparation) return;
        assistant.preparingRequest = null;
        if (!assistantProviderAvailable()) {
            assistant.error = `${assistant.provider === "hermes" ? "Hermes" : "Codex"} is unavailable.`;
            refreshAssistant();
            return;
        }
        const sceneId = String(requestShot.id || `clip_${String(requestSceneIndex + 1).padStart(4, "0")}`);
        const sceneKey = promptSceneKey(sceneId, requestSceneIndex);
        const requestId = `pa-${globalThis.crypto?.randomUUID?.()
            ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`}`;
        const request = makePromptAssistRequest({
            requestId,
            conversationId: assistant.client.conversationId,
            provider: assistant.provider,
            mode: assistant.mode,
            instruction: assistant.composer,
            context,
        });
        assistant.requestContexts.set(requestId, {
            sceneKey,
            sceneId,
            sceneIndex: requestSceneIndex,
            sourcePrompt,
            sourceRevision: request.source_revision,
            provider: assistant.provider,
        });
        const requestMeta = assistant.requestContexts.get(requestId);
        assistant.activeRequest = requestId;
        persistPendingRequest(requestId, requestMeta);
        assistant.status = "working";
        assistant.statusDetail = `Asking ${assistant.provider === "hermes" ? "Hermes" : "Codex"}…`;
        assistant.error = "";
        assistantMessage("user", request.instruction);
        assistant.composer = "";
        refreshAssistant();
        try {
            await assistant.client.send(request);
        } catch (error) {
            if (assistant.activeRequest !== requestId) return;
            assistant.activeRequest = null;
            assistant.requestContexts.delete(requestId);
            clearPendingRequest(requestId);
            clearAssistantReconnect();
            assistant.status = "disconnected";
            assistant.error = error.message || String(error);
            assistantMessage("system", `Could not send: ${assistant.error}`);
            refreshAssistant();
        }
    }

    function stopAssistant() {
        if (!assistant.activeRequest) return;
        if (!assistant.client.cancel(assistant.activeRequest)) {
            assistant.error = "The bridge is disconnected; the local request may finish without being delivered.";
            refreshAssistant();
        } else {
            assistant.statusDetail = "Stopping agent…";
            refreshAssistant();
        }
    }

    function resetAssistantChat() {
        if (assistant.activeRequest) assistant.client.cancel(assistant.activeRequest);
        assistant.client.reset();
        assistant.activeRequest = null;
        assistant.preparingRequest = null;
        clearPendingRequest();
        clearAssistantReconnect();
        assistant.requestContexts.clear();
        assistant.messagesByProvider = {codex: [], hermes: []};
        assistant.error = "";
        assistant.statusDetail = "New isolated conversation";
        refreshAssistant();
    }

    function copyAssistantDraft(draft) {
        const operation = navigator.clipboard?.writeText?.(draft.proposed);
        if (!operation) {
            assistant.error = "Clipboard access is unavailable. Select the proposed text and copy it manually.";
            refreshAssistant();
            return;
        }
        operation.then(() => {
            assistantMessage("system", "Proposed prompt copied.");
            refreshAssistant();
        }).catch(() => {
            assistant.error = "Clipboard access was refused. Select the proposed text and copy it manually.";
            refreshAssistant();
        });
    }

    function applyAssistantDraft(draft, promptTextarea, conflict) {
        if (conflict.stale && !window.confirm(
            `${conflict.reason}\n\nApply this draft anyway and replace the current scene prompt?`,
        )) return;
        const before = promptTextarea.value;
        const after = draft.proposed;
        if (!String(after).trim()) {
            assistant.error = "An empty assistant draft cannot replace the scene prompt.";
            refreshAssistant();
            return;
        }
        assistant.lastApplied = {
            sceneKey: promptSceneKey(draft.sceneId, draft.sceneIndex),
            before,
            after,
        };
        promptTextarea.value = after;
        promptTextarea.dispatchEvent(new Event("input", {bubbles: true}));
        assistant.drafts.delete(promptSceneKey(draft.sceneId, draft.sceneIndex));
        assistantMessage("system", "Draft applied to the active scene and saved in the connected Plan.");
        refreshAssistant();
    }

    function undoAssistantApply(promptTextarea, sceneKey) {
        const undo = assistant.lastApplied;
        if (!undo || undo.sceneKey !== sceneKey || promptTextarea.value !== undo.after) return;
        promptTextarea.value = undo.before;
        promptTextarea.dispatchEvent(new Event("input", {bubbles: true}));
        assistant.lastApplied = null;
        assistantMessage("system", "The last assistant apply was undone.");
        refreshAssistant();
    }

    function renderAssistant(host, promptTextarea) {
        host.replaceChildren();
        const shot = state.plan.shots[state.active];
        const sceneId = String(shot.id || `clip_${String(state.active + 1).padStart(4, "0")}`);
        const sceneKey = promptSceneKey(sceneId, state.active);

        const head = element("div", "h3sp-assist-head");
        const heading = element("span", "h3sp-assist-title", "Prompt Assistant");
        const busy = Boolean(assistant.activeRequest || assistant.preparingRequest);
        const statusText = busy
            ? assistant.statusDetail || "Agent is working…"
            : assistant.statusDetail;
        head.append(heading, element("span", "h3sp-assist-status", statusText));

        const controls = element("div", "h3sp-assist-controls");
        const provider = element("select");
        provider.title = "Choose which isolated local agent handles this prompt turn.";
        for (const [id, label] of [["codex", "Codex"], ["hermes", "Hermes"]]) {
            const option = element("option", "", label);
            option.value = id;
            const available = assistant.providers?.find((item) => item.id === id)?.available;
            if (available === false) {
                option.textContent = `${label} (not found)`;
                option.disabled = true;
            }
            provider.append(option);
        }
        provider.value = assistant.provider;
        provider.disabled = busy;
        provider.addEventListener("change", () => {
            assistant.provider = provider.value;
            node.properties[ASSIST_PROVIDER_PROPERTY] = assistant.provider;
            persistView();
            refreshAssistant();
        });
        const mode = element("select");
        mode.title = "Choose the kind of help to request.";
        for (const item of PROMPT_ASSIST_MODES) {
            const option = element("option", "", item.label);
            option.value = item.id;
            mode.append(option);
        }
        mode.value = assistant.mode;
        mode.disabled = busy;
        mode.addEventListener("change", () => {
            assistant.mode = mode.value;
            node.properties[ASSIST_MODE_PROPERTY] = assistant.mode;
            persistView();
            refreshAssistant();
        });
        controls.append(provider, mode, button("New chat", "Clear agent conversation; staged drafts remain.", resetAssistantChat));

        const chat = element("div", "h3sp-assist-chat");
        const messages = messagesForProvider();
        if (!messages.length) {
            chat.append(element(
                "div", "h3sp-assist-empty",
                "Ask for a rewrite, continuity pass, critique, or a specific change. Nothing is applied until you press Apply.",
            ));
        } else {
            for (const message of messages) {
                chat.append(element(
                    "div",
                    `h3sp-assist-message h3sp-assist-message-${message.role}`,
                    message.text,
                ));
            }
            setTimeout(() => { chat.scrollTop = chat.scrollHeight; }, 0);
        }

        const draft = assistant.drafts.get(sceneKey);
        let draftPanel = null;
        if (draft) {
            const conflict = draftConflict(draft, sceneId, promptTextarea.value);
            draftPanel = element("div", "h3sp-assist-draft");
            const draftHead = element("div", "h3sp-assist-draft-head");
            draftHead.append(
                element("span", "h3sp-assist-draft-title", `Staged ${draft.provider || "agent"} proposal`),
                conflict.stale ? element("span", "h3sp-assist-stale", conflict.reason) : element("span"),
            );
            const proposed = element("textarea");
            proposed.value = draft.proposed;
            proposed.title = "You can edit the staged proposal before applying it.";
            proposed.addEventListener("input", () => { draft.proposed = proposed.value; });
            const original = element("details", "h3sp-assist-original");
            original.append(
                element("summary", "", "Compare with original"),
                element("pre", "", draft.sourcePrompt),
            );
            const actions = element("div", "h3sp-assist-actions");
            actions.append(
                button(
                    conflict.stale ? "Apply anyway…" : "Apply to scene",
                    "Replace the real active scene prompt in the connected Plan.",
                    () => applyAssistantDraft(draft, promptTextarea, conflict),
                ),
                button("Copy", "Copy the proposed prompt without changing the Plan.", () => copyAssistantDraft(draft)),
                button("Discard", "Remove this staged proposal without changing the Plan.", () => {
                    assistant.drafts.delete(sceneKey);
                    refreshAssistant();
                }),
            );
            draftPanel.append(draftHead, proposed, original, actions);
        }

        const contexts = element("div", "h3sp-assist-contexts");
        const contextToggle = (label, checked, change, title) => {
            const wrapper = element("label");
            wrapper.title = title;
            const input = element("input");
            input.type = "checkbox";
            input.checked = checked;
            input.disabled = busy;
            input.addEventListener("change", () => change(input.checked));
            wrapper.append(input, document.createTextNode(label));
            return wrapper;
        };
        contexts.append(
            contextToggle("Shared prompt", assistant.includeShared, (value) => {
                assistant.includeShared = value;
            }, "Include the Plan's shared/global prompt as read-only context."),
            contextToggle("Previous + next", assistant.includeAdjacent, (value) => {
                assistant.includeAdjacent = value;
            }, "Include adjacent scene prompts for continuity advice."),
            element("span", "h3sp-assist-status", "Selected text is included automatically"),
        );

        const compose = element("div", "h3sp-assist-compose");
        const composer = element("textarea");
        composer.value = assistant.composer;
        composer.placeholder = PROMPT_ASSIST_DEFAULT_INSTRUCTIONS[assistant.mode];
        composer.disabled = busy;
        composer.addEventListener("input", () => { assistant.composer = composer.value; });
        composer.addEventListener("keydown", (event) => {
            if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
                event.preventDefault();
                void sendAssistant(promptTextarea);
            }
        });
        const send = button("Ask agent", "Send (Ctrl/Cmd+Enter). The response is staged, never auto-applied.", () => {
            void sendAssistant(promptTextarea);
        });
        send.disabled = busy || !assistantProviderAvailable();
        compose.append(composer, send);
        if (assistant.activeRequest) {
            compose.append(button("Stop", "Interrupt this prompt-assist request.", stopAssistant));
        }

        const error = assistant.error
            ? element("div", "h3sp-assist-error", assistant.error) : null;
        const undo = assistant.lastApplied;
        const undoButton = undo?.sceneKey === sceneKey && promptTextarea.value === undo.after
            ? button("Undo last apply", "Restore the prompt that existed before the last assistant Apply.", () => {
                undoAssistantApply(promptTextarea, sceneKey);
            }) : null;

        host.append(head, controls, chat);
        if (draftPanel) host.append(draftPanel);
        host.append(contexts, compose);
        if (undoButton) host.append(undoButton);
        if (error) host.append(error);
    }

    function showFailure(message) {
        assistant.host = null;
        root.replaceChildren();
        root.append(
            element("div", "h3sp-title", "MiniMax H3 Scene Prompt Editor"),
            element("div", "h3sp-error", message),
            element("div", "h3sp-context", "Connect the Plan output to this node's plan input."),
        );
    }

    function navigate(offset, absolute = null) {
        if (!state.plan?.shots?.length) return;
        const requested = absolute == null ? state.active + offset : Number(absolute);
        state.active = Math.max(0, Math.min(state.plan.shots.length - 1, requested));
        persistView();
        render();
        root.querySelector(".h3sp-textarea")?.focus();
    }

    function showReferencePreview(record, preview) {
        preview.replaceChildren();
        preview.classList.add("h3sp-visible");
        const mediaKind = record.kind === "picture" ? "image" : record.kind;
        const media = findMediaPreview(record.source, mediaKind);
        if (media.url) {
            const mediaElement = element(mediaKind === "image" ? "img"
                : mediaKind === "video" ? "video" : "audio",
            "h3sp-ref-preview-media");
            mediaElement.src = media.url;
            if (mediaKind !== "image") {
                mediaElement.controls = true;
                mediaElement.preload = "metadata";
            } else {
                mediaElement.alt = `Preview for ${record.token}`;
                mediaElement.loading = "lazy";
            }
            preview.append(mediaElement);
        } else {
            preview.append(element(
                "div", "h3sp-ref-preview-copy",
                "No browser-playable source was found. Dynamic tensors can " +
                "still run in the generation graph, but need an upstream loaded file for an editor preview.",
            ));
        }

        const sourceTitle = media.source?.title || nodeType(media.source) || "unresolved source";
        const mapping = record.active
            ? `${record.label} in scene ${state.active + 1}`
            : `inactive in scene ${state.active + 1}`;
        const previewTitle = record.mode === "native"
            ? record.token : `${record.token} → ${mapping}`;
        const copy = element("div", "h3sp-ref-preview-copy");
        copy.append(
            element("div", "h3sp-ref-preview-title", previewTitle),
            document.createTextNode(
                `\n${record.kind.toUpperCase()} · scenes ${record.selector}` +
                `\nSource: ${sourceTitle}`,
            ),
        );
        preview.append(copy);
    }

    function renderReferenceTray(refs, textarea) {
        refs.replaceChildren();
        const {records, mode, wrapper} = availableReferenceRecords(
            node, state.active + 1,
        );
        const preview = element("div", "h3sp-ref-preview");
        if (!records.length) {
            refs.append(element(
                "div", "h3sp-ref-help",
                wrapper
                    ? `No connected references are active in scene ${state.active + 1}.`
                    : "No connected Scheduled Ref2VA, core Ref2VA, or core I2V/FL2V references were found. The menu does not invent unavailable labels.",
            ));
            return;
        }

        const help = mode === "scheduled"
            ? `Scheduled references for scene ${state.active + 1}. Hover to preview; ` +
              "click to insert the optional stable @alias. It compiles to a native label; " +
              "the scheduler inserts no prompt text. Audio never autoplays."
            : mode === "native_keyframes"
              ? `Core I2V/FL2V keyframes for scene ${state.active + 1}. ` +
                "A first-scene gate is shown inactive on continuations. " +
                "Hover to preview; click to insert the native Picture label."
              : "Core Ref2VA references. Hover to preview; click to insert the " +
                "native label. Audio never autoplays.";
        refs.append(element("div", "h3sp-ref-help", help));
        const icons = {picture: "▧", video: "▶", audio: "♫"};
        for (const record of records) {
            const mapping = (mode === "scheduled" || mode === "native_keyframes")
                ? (mode === "scheduled" ? ` → ${record.label}` : "") : "";
            const chip = button(
                `${icons[record.kind] ?? "@"} ${record.token}${mapping}`,
                mode === "scheduled"
                    ? `Insert optional alias ${record.token}; it compiles to ${record.label} in this scene.`
                    : `Insert ${record.token} for the connected core conditioning input.`,
                () => {
                    insertText(textarea, record.token);
                    refs.classList.remove("h3sp-open");
                },
            );
            chip.classList.add("h3sp-ref-chip", "h3sp-active");
            chip.addEventListener("mouseenter", () => showReferencePreview(record, preview));
            chip.addEventListener("focus", () => showReferencePreview(record, preview));
            refs.append(chip);
        }
        refs.append(preview);
        showReferencePreview(records[0], preview);
    }

    function render() {
        if (!state.plan?.shots?.length) {
            showFailure("The connected Plan has no scenes.");
            return;
        }
        state.active = Math.max(0, Math.min(state.active, state.plan.shots.length - 1));
        root.style.setProperty("--h3sp-font-size", `${state.fontSize}px`);
        assistant.host = null;
        root.replaceChildren();

        const shot = state.plan.shots[state.active];
        const shotId = String(shot.id || `clip_${String(state.active + 1).padStart(4, "0")}`);
        const head = element("div", "h3sp-head");
        head.append(
            element("span", "h3sp-title", "Scene Prompt Editor"),
            element("span", "h3sp-context", sharedPrompt(state.plan).text.trim()
                ? "Shared prompt active (unchanged)" : "No shared prompt"),
        );

        const nav = element("div", "h3sp-nav");
        const previous = button("←", "Previous scene (Alt+Left)", () => navigate(-1));
        const next = button("→", "Next scene (Alt+Right)", () => navigate(1));
        previous.disabled = state.active === 0;
        next.disabled = state.active === state.plan.shots.length - 1;
        const sceneSelect = element("select");
        for (let index = 0; index < state.plan.shots.length; index += 1) {
            const option = element("option", "", `Scene ${index + 1} — ${state.plan.shots[index].id || `clip_${String(index + 1).padStart(4, "0")}`}`);
            option.value = String(index);
            sceneSelect.append(option);
        }
        sceneSelect.value = String(state.active);
        sceneSelect.title = "Jump directly to another scene prompt.";
        sceneSelect.addEventListener("change", () => navigate(0, sceneSelect.value));

        const font = element("div", "h3sp-font");
        const fontValue = element("span", "h3sp-font-value", `${state.fontSize}px`);
        const smaller = button("A−", "Decrease editor font size", () => {
            state.fontSize = clamp(state.fontSize - 2, MIN_FONT_SIZE, MAX_FONT_SIZE, DEFAULT_FONT_SIZE);
            persistView();
            render();
        });
        const larger = button("A+", "Increase editor font size", () => {
            state.fontSize = clamp(state.fontSize + 2, MIN_FONT_SIZE, MAX_FONT_SIZE, DEFAULT_FONT_SIZE);
            persistView();
            render();
        });
        smaller.disabled = state.fontSize <= MIN_FONT_SIZE;
        larger.disabled = state.fontSize >= MAX_FONT_SIZE;
        font.append(smaller, fontValue, larger);
        nav.append(previous, sceneSelect, next, font);

        const textarea = element("textarea", "h3sp-textarea");
        textarea.value = promptValueToText(shot.prompt, `Scene ${state.active + 1} prompt`);
        textarea.placeholder = "Write this scene's action, camera, performance, dialogue, and ending continuity…";
        textarea.spellcheck = true;
        textarea.title = "This is the actual active scene prompt in the connected H3 Chain Plan.";

        const tools = element("div", "h3sp-tools");
        const refs = element("div", "h3sp-refs");
        const referenceButton = button("@ Reference", "Open connected Ref2VA references and previews (@)", () => {
            const opening = !refs.classList.contains("h3sp-open");
            if (opening) renderReferenceTray(refs, textarea);
            refs.classList.toggle("h3sp-open", opening);
        });
        const dialogueButton = button("# Dialogue", "Wrap selection in <d> dialogue tags (#)", () => {
            insertDialogue(textarea);
        });
        tools.append(
            referenceButton,
            dialogueButton,
            element("span", "h3sp-hint", "Alt+←/→ scenes · @ refs · # dialogue"),
        );
        const footer = element("div", "h3sp-footer");
        const identity = element(
            "span", "", `Scene ${state.active + 1}/${state.plan.shots.length} · ${shotId}`,
        );
        const status = element("span", "", "Synchronized with Plan");
        footer.append(identity, status);

        textarea.addEventListener("input", () => {
            shot.prompt = promptTextToLines(textarea.value);
            writePlan(status);
            refreshAssistant();
        });
        textarea.addEventListener("keydown", (event) => {
            if (event.altKey && event.key === "ArrowLeft") {
                event.preventDefault();
                navigate(-1);
            } else if (event.altKey && event.key === "ArrowRight") {
                event.preventDefault();
                navigate(1);
            } else if (!event.ctrlKey && !event.metaKey && !event.altKey && event.key === "@") {
                event.preventDefault();
                renderReferenceTray(refs, textarea);
                refs.classList.add("h3sp-open");
                refs.querySelector(".h3sp-ref-chip, button")?.focus();
            } else if (!event.ctrlKey && !event.metaKey && !event.altKey && event.key === "#") {
                event.preventDefault();
                insertDialogue(textarea);
            }
        });

        root.append(head, nav, tools, refs, textarea);
        if (PROMPT_ASSISTANT_ENABLED) {
            const assistantHost = element("div", "h3sp-assist");
            assistant.host = assistantHost;
            renderAssistant(assistantHost, textarea);
            root.append(assistantHost);
        }
        root.append(footer);
    }

    function loadPlan(force = false) {
        const planNode = upstreamPlanNode(node);
        const planWidget = planNode?.widgets?.find((item) => item.name === "plan_json");
        if (!planNode || !planWidget) {
            if (force || state.planNode) {
                state.plan = null;
                state.planNode = null;
                state.planWidget = null;
                state.lastValue = "";
                showFailure("No connected H3 Chain Plan was found.");
            }
            return;
        }
        const value = String(planWidget.value ?? "");
        if (!force && planNode === state.planNode && value === state.lastValue) return;
        try {
            state.plan = parsePlanJson(value);
            state.planNode = planNode;
            state.planWidget = planWidget;
            state.lastValue = value;
            render();
        } catch (error) {
            showFailure(`Connected Plan JSON is invalid:\n${error.message}`);
        }
    }

    const widget = node.addDOMWidget(
        "h3_scene_prompt_editor", "h3-scene-prompt-editor", root,
        {
            serialize: false,
            hideOnZoom: false,
            getMinHeight: () => PROMPT_ASSISTANT_ENABLED ? 760 : 420,
        },
    );
    widget.serialize = false;
    const minimumWidth = PROMPT_ASSISTANT_ENABLED ? 760 : 700;
    const minimumHeight = PROMPT_ASSISTANT_ENABLED ? 900 : 620;
    const currentWidth = Number(node.size?.[0]);
    const currentHeight = Number(node.size?.[1]);
    // Undo only the exact assistant-era default dimensions. Preserve any node
    // the user deliberately made larger.
    const targetWidth = !PROMPT_ASSISTANT_ENABLED && currentWidth === 760
        ? minimumWidth : Math.max(currentWidth || minimumWidth, minimumWidth);
    const targetHeight = !PROMPT_ASSISTANT_ENABLED && currentHeight === 900
        ? minimumHeight : Math.max(currentHeight || minimumHeight, minimumHeight);
    node.setSize?.([
        targetWidth,
        targetHeight,
    ]);

    const connectionsChanged = node.onConnectionsChange;
    node.onConnectionsChange = function () {
        const result = connectionsChanged?.apply(this, arguments);
        setTimeout(() => loadPlan(true), 0);
        return result;
    };
    const removed = node.onRemoved;
    node.onRemoved = function () {
        if (state.pollTimer != null) window.clearInterval(state.pollTimer);
        if (PROMPT_ASSISTANT_ENABLED) {
            assistant.preparingRequest = null;
            clearAssistantReconnect();
            clearPendingRequest();
            assistant.client?.close();
        }
        return removed?.apply(this, arguments);
    };
    node._h3ScenePromptEditorRefresh = () => loadPlan(true);
    state.pollTimer = window.setInterval(() => loadPlan(false), 500);
    loadPlan(true);
    if (PROMPT_ASSISTANT_ENABLED) restorePendingRequest();
}

app.registerExtension({
    name: "minimax_h3_context_loop.scene_prompt_editor",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_NAME) return;
        const created = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = created?.apply(this, arguments);
            setTimeout(() => mount(this), 0);
            return result;
        };
    },
    async nodeCreated(node) {
        if (nodeType(node) === NODE_NAME) mount(node);
    },
    async afterConfigureGraph() {
        for (const node of allNodes(app.graph)) {
            if (nodeType(node) === NODE_NAME) {
                setTimeout(() => node._h3ScenePromptEditorRefresh?.(), 0);
            }
        }
    },
});
