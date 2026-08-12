import {app} from "/scripts/app.js";
import {api} from "/scripts/api.js";
import {
    MAX_SHOTS,
    automaticSceneColor,
    calculatePlanTiming,
    derivedSceneSeed,
    duplicateShot,
    formatClock,
    h3FrameLength,
    makeShot,
    moveShot,
    parsePlanJson,
    planToJson,
    promptTextToLines,
    promptValueToText,
    randomSceneSeed,
    safeShotId,
    setSharedPrompt,
    sharedPrompt,
} from "./h3_chain_plan_core.mjs";
import {availableReferenceRecords} from "./h3_reference_preview_core.mjs";

// This scene editor is an original implementation. Its quick @ reference and
// # dialogue interactions are inspired by nkxx188/ComfyUI-MiniMaxH3-Easy,
// distributed under the MIT License. See THIRD_PARTY_NOTICES.md.

const EXTENSION = "minimax_h3_context_loop.chain_plan_editor";
const NODE_NAME = "MiniMaxH3ChainPlan";
const MIN_WIDTH = 700;
const EDITOR_HEIGHT = 650;
const SCENE_COLOR_PROPERTY = "h3_chain_scene_colors";
const LAYOUT_PROPERTY = "h3_chain_plan_layout";

function injectStyles() {
    if (document.getElementById("h3-chain-plan-editor-style")) return;
    const style = document.createElement("style");
    style.id = "h3-chain-plan-editor-style";
    style.textContent = `
        .h3c-editor {
            --h3c-bg: color-mix(in srgb, var(--comfy-menu-bg, #202124) 90%, #111827);
            --h3c-panel: color-mix(in srgb, var(--comfy-input-bg, #111827) 84%, #24304a);
            --h3c-border: color-mix(in srgb, var(--border-color, #555) 70%, #7c8db5);
            --h3c-text: var(--input-text, #e8eaf0);
            --h3c-muted: color-mix(in srgb, var(--h3c-text) 58%, transparent);
            --h3c-accent: #7fa8ff;
            box-sizing: border-box;
            min-height: 0;
            overflow: auto;
            padding: 10px;
            border: 1px solid var(--h3c-border);
            border-radius: 8px;
            background: var(--h3c-bg);
            color: var(--h3c-text);
            font: 12px/1.35 system-ui, sans-serif;
            scrollbar-gutter: stable;
        }
        .h3c-editor *, .h3c-editor *::before, .h3c-editor *::after { box-sizing: border-box; }
        .h3c-editor input, .h3c-editor textarea, .h3c-editor select, .h3c-editor button {
            color: var(--h3c-text);
            font: inherit;
        }
        .h3c-editor input, .h3c-editor textarea, .h3c-editor select {
            width: 100%;
            min-width: 0;
            padding: 6px 7px;
            border: 1px solid var(--h3c-border);
            border-radius: 5px;
            background: var(--comfy-input-bg, #15171d);
        }
        .h3c-editor textarea { resize: vertical; line-height: 1.45; }
        .h3c-editor button {
            padding: 5px 8px;
            border: 1px solid var(--h3c-border);
            border-radius: 5px;
            background: var(--comfy-input-bg, #252832);
            cursor: pointer;
            white-space: nowrap;
        }
        .h3c-editor button:hover { border-color: var(--h3c-accent); }
        .h3c-editor button:disabled { cursor: not-allowed; opacity: .4; }
        .h3c-header, .h3c-toolbar, .h3c-card-head, .h3c-prompt-tools,
        .h3c-json-actions, .h3c-footer { display: flex; align-items: center; gap: 6px; }
        .h3c-header { justify-content: space-between; margin-bottom: 8px; }
        .h3c-header-actions { display:flex; align-items:center; justify-content:flex-end;
            gap:7px; min-width:0; }
        .h3c-open-output { display:inline-flex; align-items:center; gap:5px;
            padding:4px 7px !important; }
        .h3c-folder-icon { width:15px; height:15px; flex:none; fill:none;
            stroke:currentColor; stroke-width:1.8; stroke-linecap:round;
            stroke-linejoin:round; }
        .h3c-title { font-size: 15px; font-weight: 700; }
        .h3c-summary { color: var(--h3c-muted); text-align: right; }
        .h3c-section {
            margin-bottom: 9px;
            padding: 9px;
            border: 1px solid var(--h3c-border);
            border-radius: 7px;
            background: var(--h3c-panel);
        }
        .h3c-label { display: block; margin-bottom: 4px; color: var(--h3c-muted); font-weight: 650; }
        .h3c-help { margin-top: 4px; color: var(--h3c-muted); }
        .h3c-defaults { display: grid; grid-template-columns: minmax(0, 1fr) 150px 130px; gap: 8px; }
        .h3c-prefix { min-height: 88px; }
        .h3c-toolbar { position: sticky; top: -10px; z-index: 4; padding: 7px 0; background: var(--h3c-bg); }
        .h3c-toolbar .h3c-spacer { flex: 1; }
        .h3c-card {
            --h3c-scene-color: var(--h3c-accent);
            margin-bottom: 9px;
            padding: 8px;
            border: 1px solid color-mix(in srgb, var(--h3c-scene-color) 58%, var(--h3c-border));
            border-left: 4px solid var(--h3c-scene-color);
            border-radius: 7px;
            background:
                linear-gradient(90deg, color-mix(in srgb, var(--h3c-scene-color) 7%, transparent), transparent 140px),
                var(--h3c-panel);
        }
        .h3c-card.h3c-invalid { border-left-color: #ff6b72; }
        .h3c-card.h3c-drag-over { outline: 2px solid var(--h3c-accent); }
        .h3c-card-head { margin-bottom: 7px; }
        .h3c-drag { cursor: grab; user-select: none; padding: 4px 3px; color: var(--h3c-muted); }
        .h3c-index { min-width: 58px; font-weight: 700; }
        .h3c-color {
            width: 23px !important;
            min-width: 23px !important;
            height: 23px;
            padding: 1px !important;
            border-radius: 50% !important;
            cursor: pointer;
        }
        .h3c-color::-webkit-color-swatch-wrapper { padding: 1px; }
        .h3c-color::-webkit-color-swatch { border: 0; border-radius: 50%; }
        .h3c-color::-moz-color-swatch { border: 0; border-radius: 50%; }
        .h3c-id { flex: 1; }
        .h3c-timing { color: var(--h3c-muted); white-space: nowrap; }
        .h3c-length-row { display: grid; grid-template-columns: 115px minmax(120px, 1fr) 190px; gap: 7px; align-items: center; margin-bottom: 7px; }
        .h3c-prompt { min-height: 112px; }
        .h3c-prompt-tools { position: relative; margin: 5px 0 2px; }
        .h3c-prompt-tools .h3c-hint { color: var(--h3c-muted); margin-left: auto; }
        .h3c-ref-menu {
            display: none;
            flex-wrap: wrap;
            gap: 4px;
            margin-top: 6px;
            padding: 6px;
            border: 1px solid var(--h3c-border);
            border-radius: 6px;
            background: var(--h3c-bg);
        }
        .h3c-ref-menu.h3c-open { display: flex; }
        .h3c-ref-menu button { padding: 3px 6px; color: var(--h3c-accent); }
        .h3c-seed-control { display:grid; grid-template-columns:minmax(210px,1fr) auto auto;
            align-items:center; gap:6px; }
        .h3c-seed-status { grid-column:1 / -1; color:var(--h3c-muted);
            overflow-wrap:anywhere; }
        .h3c-advanced-fields { display: none; grid-template-columns:minmax(220px, 1fr); gap: 7px; margin-top: 8px; }
        .h3c-editor.h3c-show-advanced .h3c-advanced-fields { display: grid; }
        .h3c-errors { display: none; margin: 7px 0; padding: 7px; border-radius: 5px; color: #ffb4b8; background: #5d202866; white-space: pre-wrap; }
        .h3c-errors.h3c-open { display: block; }
        .h3c-json-panel { display: none; }
        .h3c-json-panel.h3c-open { display: block; }
        .h3c-json { min-height: 260px; font-family: ui-monospace, SFMono-Regular, Consolas, monospace !important; }
        .h3c-json-actions { margin-top: 6px; }
        .h3c-json-status { color: var(--h3c-muted); }
        .h3c-footer { justify-content: space-between; padding-top: 4px; color: var(--h3c-muted); }
        .h3c-footer a { color: var(--h3c-accent); }
        @media (max-width: 650px) {
            .h3c-defaults, .h3c-length-row, .h3c-advanced-fields,
            .h3c-seed-control { grid-template-columns: 1fr; }
            .h3c-seed-status { grid-column:1; }
            .h3c-card-head { flex-wrap: wrap; }
            .h3c-timing { width: 100%; }
        }
    `;
    document.head.appendChild(style);
}

function element(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
}

function button(label, title, action) {
    const item = element("button", "", label);
    item.type = "button";
    if (title) item.title = title;
    item.addEventListener("click", action);
    return item;
}

function folderOpenIcon() {
    const namespace = "http://www.w3.org/2000/svg";
    const icon = document.createElementNS(namespace, "svg");
    icon.classList.add("h3c-folder-icon");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("aria-hidden", "true");
    const folder = document.createElementNS(namespace, "path");
    folder.setAttribute(
        "d",
        "M3.5 8.5V6.25A1.75 1.75 0 0 1 5.25 4.5h4.1l2.1 2.25h7.3a1.75 1.75 0 0 1 1.75 1.75v1",
    );
    const opening = document.createElementNS(namespace, "path");
    opening.setAttribute(
        "d",
        "M4.5 9.5h15.35a1.35 1.35 0 0 1 1.3 1.72l-2.05 7.1a1.6 1.6 0 0 1-1.54 1.18H5.4a1.6 1.6 0 0 1-1.57-1.3L2.9 13.15A3.1 3.1 0 0 1 4.5 9.5Z",
    );
    icon.append(folder, opening);
    return icon;
}

function setOutputButtonLabel(item, label, showIcon = false) {
    item.replaceChildren();
    if (showIcon) item.append(folderOpenIcon());
    item.append(document.createTextNode(label));
}

function field(label, control) {
    const wrap = element("label", "");
    wrap.append(element("span", "h3c-label", label), control);
    return wrap;
}

function numberInput(value, options = {}) {
    const input = element("input");
    input.type = "number";
    input.value = value ?? "";
    for (const [key, setting] of Object.entries(options)) input[key] = setting;
    return input;
}

function widgetValue(node, name, fallback) {
    const widget = node.widgets?.find((item) => item.name === name);
    return widget?.value ?? fallback;
}

function collapseWidget(widget) {
    widget._h3OriginalType ??= widget.type;
    widget._h3OriginalComputeSize ??= widget.computeSize;
    widget.type = "hidden";
    widget.computeSize = () => [0, -4];

    // Multiline STRING widgets are real DOM textareas in current ComfyUI.
    // Collapsing only the LiteGraph geometry leaves that textarea floating at
    // its previous coordinates, over the scene editor. Hide both possible DOM
    // handles while retaining widget.value for workflow serialization.
    const elements = new Set([widget.inputEl, widget.element]);
    for (const item of elements) {
        if (!item?.style) continue;
        item.style.setProperty("display", "none", "important");
        item.setAttribute?.("aria-hidden", "true");
    }
}

function colorOverrides(node) {
    node.properties ??= {};
    const current = node.properties[SCENE_COLOR_PROPERTY];
    if (!current || typeof current !== "object" || Array.isArray(current)) {
        node.properties[SCENE_COLOR_PROPERTY] = {};
    }
    return node.properties[SCENE_COLOR_PROPERTY];
}

function planLayout(node) {
    node.properties ??= {};
    const current = node.properties[LAYOUT_PROPERTY];
    if (!current || typeof current !== "object" || Array.isArray(current)) {
        node.properties[LAYOUT_PROPERTY] = {};
    }
    const layout = node.properties[LAYOUT_PROPERTY];
    if (!layout.promptHeights || typeof layout.promptHeights !== "object"
            || Array.isArray(layout.promptHeights)) {
        layout.promptHeights = {};
    }
    return layout;
}

function sceneColorKey(shot, index) {
    return safeShotId(shot?.id, `clip_${String(index + 1).padStart(4, "0")}`);
}

function sceneColor(node, shot, index) {
    const override = colorOverrides(node)[sceneColorKey(shot, index)];
    return /^#[0-9a-f]{6}$/i.test(String(override || ""))
        ? String(override).toLowerCase()
        : automaticSceneColor(index);
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
    const offset = selected ? markup.length : 3;
    insertText(textarea, markup, offset);
}

function lengthMode(shot) {
    if (shot.length !== undefined || shot.frames !== undefined) return "frames";
    if (shot.duration_seconds !== undefined) return "seconds";
    return "default";
}

function updateShotLengthMode(shot, mode, fallbackSeconds) {
    delete shot.length;
    delete shot.frames;
    delete shot.duration_seconds;
    const numericFallback = Number(fallbackSeconds);
    const seconds = Number.isFinite(numericFallback) && numericFallback > 0
        ? numericFallback : 15;
    if (mode === "seconds") shot.duration_seconds = seconds;
    if (mode === "frames") shot.length = h3FrameLength(seconds);
}

function downloadJson(value, filename) {
    const blob = new Blob([value], {type: "application/json"});
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
}

function mountEditor(node) {
    if (node._h3ChainEditor || typeof node.addDOMWidget !== "function") return;
    const planWidget = node.widgets?.find((widget) => widget.name === "plan_json");
    if (!planWidget) return;

    injectStyles();
    const root = element("div", "h3c-editor");
    root.title = "Build an ordered MiniMax H3 scene plan. Hover individual controls for wiring, timing, and formatting guidance.";
    for (const eventName of [
        "pointerdown", "pointerup", "mousedown", "mouseup", "click", "dblclick",
    ]) {
        root.addEventListener(eventName, (event) => event.stopPropagation());
    }
    root.addEventListener("wheel", (event) => event.stopPropagation());
    const savedLayout = planLayout(node);
    const state = {
        plan: null,
        advanced: Boolean(savedLayout.advanced),
        jsonOpen: Boolean(savedLayout.jsonOpen),
        lastWidgetValue: "",
        syncing: false,
        draggedIndex: null,
        resizeObservers: [],
        seedRefreshers: [],
    };
    node._h3ChainEditor = state;

    collapseWidget(planWidget);

    const domWidget = node.addDOMWidget("h3_chain_scene_editor", "h3-chain-editor", root, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => EDITOR_HEIGHT,
    });
    domWidget.serialize = false;

    function graphDirty() {
        node.graph?.setDirtyCanvas?.(true, true);
        app.graph?.setDirtyCanvas?.(true, true);
    }

    function savePanelState() {
        const layout = planLayout(node);
        layout.advanced = state.advanced;
        layout.jsonOpen = state.jsonOpen;
        graphDirty();
    }

    function disconnectResizeObservers() {
        for (const observer of state.resizeObservers) observer.disconnect?.();
        state.resizeObservers = [];
    }

    function bindTextareaHeight(textarea, key, minimum) {
        const heights = planLayout(node).promptHeights;
        const saved = Number(heights[key]);
        if (Number.isFinite(saved) && saved >= minimum) {
            textarea.style.height = `${Math.round(saved)}px`;
        }
        if (typeof ResizeObserver !== "function") return;
        let initialized = false;
        const observer = new ResizeObserver(() => {
            if (!textarea.isConnected) return;
            const next = Math.round(textarea.offsetHeight);
            if (!Number.isFinite(next) || next < minimum) return;
            if (!initialized) {
                initialized = true;
                return;
            }
            const current = planLayout(node).promptHeights;
            if (Number(current[key]) === next) return;
            current[key] = next;
            node.properties[LAYOUT_PROPERTY] = {
                ...planLayout(node), promptHeights: {...current},
            };
            graphDirty();
        });
        observer.observe(textarea);
        state.resizeObservers.push(observer);
    }

    function saveSceneColor(key, value) {
        const colors = colorOverrides(node);
        if (value) colors[key] = value.toLowerCase();
        else delete colors[key];
        node.properties[SCENE_COLOR_PROPERTY] = {...colors};
        graphDirty();
    }

    function applyResponsiveSize() {
        collapseWidget(planWidget);
        const width = Math.max(Number(node.size?.[0]) || MIN_WIDTH, MIN_WIDTH);
        // Preserve the workflow/user-selected height. The DOM widget receives
        // all free vertical space above its minimum, so taller nodes reveal
        // more scene cards and shorter nodes scroll internally.
        const height = Math.max(Number(node.size?.[1]) || 0, EDITOR_HEIGHT + 120);
        // Call even when dimensions are unchanged so ComfyUI recomputes the
        // DOM widget's free-space allocation after plan_json was collapsed.
        node.setSize?.([width, height]);
        graphDirty();
    }

    function scheduleResponsiveSize() {
        // Workflow configuration restores serialized dimensions after
        // onNodeCreated. Reallocate the DOM viewport once layout has settled.
        requestAnimationFrame(() => requestAnimationFrame(applyResponsiveSize));
        setTimeout(applyResponsiveSize, 150);
    }

    function currentSettings() {
        return {
            contextLength: widgetValue(node, "context_length", 22),
            anchorMode: widgetValue(node, "anchor_mode", "head"),
            defaultDurationSeconds: widgetValue(node, "default_duration_seconds", 15),
            defaultSteps: widgetValue(node, "default_steps", 20),
        };
    }

    function timing() {
        return calculatePlanTiming(state.plan, currentSettings());
    }

    function syncPlan() {
        if (!state.plan) return;
        const value = planToJson(state.plan);
        state.syncing = true;
        planWidget.value = value;
        state.lastWidgetValue = value;
        state.syncing = false;
        const jsonArea = root.querySelector(".h3c-json");
        if (jsonArea && document.activeElement !== jsonArea) jsonArea.value = value;
        updateTiming();
        graphDirty();
    }

    function updateTiming() {
        if (!state.plan) return;
        const result = timing();
        const summary = root.querySelector(".h3c-summary");
        if (summary) {
            summary.textContent = `${result.shots.length} scenes · ${result.totalFrames} delivered frames · ${formatClock(result.totalSeconds)}`;
        }
        for (const row of result.shots) {
            const card = root.querySelector(`.h3c-card[data-index="${row.index - 1}"]`);
            if (!card) continue;
            const label = card.querySelector(".h3c-timing");
            if (label) {
                label.textContent = `${row.rawFrames || "—"} raw / ${row.deliveredFrames || "—"} delivered · ${formatClock(row.deliveredSeconds)}`;
                label.title = row.errors.join("\n") || `Generation starts at delivered frame ${row.generationStartFrame}.`;
            }
            card.classList.toggle("h3c-invalid", row.errors.length > 0);
        }
        const errors = root.querySelector(".h3c-errors");
        if (errors) {
            errors.textContent = result.errors.join("\n");
            errors.classList.toggle("h3c-open", result.errors.length > 0);
        }
    }

    function promptTools(textarea, scene = null) {
        const wrap = element("div", "h3c-prompt-tools");
        const references = button("@ Reference", "Insert a MiniMax reference tag", () => {
            const opening = !menu.classList.contains("h3c-open");
            if (opening) renderReferenceMenu();
            menu.classList.toggle("h3c-open", opening);
        });
        const dialogue = button("# Dialogue", "Wrap the selection in <d> dialogue tags", () => {
            insertDialogue(textarea);
        });
        const hint = element("span", "h3c-hint", "Shortcuts: @ reference · # dialogue");
        const menu = element("div", "h3c-ref-menu");
        function renderReferenceMenu() {
            menu.replaceChildren();
            const requestedScene = scene ?? 1;
            const {records, mode} = availableReferenceRecords(
                node, requestedScene, {includeInactive: scene == null},
            );
            if (!records.length) {
                menu.append(element(
                    "span", "h3c-help",
                    scene == null
                        ? "No references connected to a downstream Ref2VA/I2V node."
                        : `No connected references are active in scene ${scene}.`,
                ));
                return;
            }
            menu.append(element(
                "span", "h3c-help",
                mode === "scheduled"
                    ? "Connected scheduled references only. @aliases are optional authoring shortcuts that compile to native labels; the scheduler inserts no prompt text."
                    : "Connected core references only. These use native <Picture/Video/Audio N> labels; @aliases are not required.",
            ));
            for (const record of records) {
                const mapping = mode === "scheduled" && record.label
                    ? ` → ${record.label}` : "";
                menu.append(button(
                    `${record.token}${mapping}`,
                    mode === "scheduled"
                        ? `Insert optional alias ${record.token}. It compiles to ${record.label ?? "the active native label"} for this scene.`
                        : `Insert ${record.token} for the connected core reference.`,
                    () => {
                        insertText(textarea, record.token);
                        menu.classList.remove("h3c-open");
                    },
                ));
            }
        }
        textarea.addEventListener("keydown", (event) => {
            if (event.ctrlKey || event.metaKey || event.altKey) return;
            if (event.key === "@") {
                event.preventDefault();
                renderReferenceMenu();
                menu.classList.add("h3c-open");
                references.focus();
            } else if (event.key === "#") {
                event.preventDefault();
                insertDialogue(textarea);
            }
        });
        wrap.append(references, dialogue, hint);
        const group = element("div");
        group.append(wrap, menu);
        return group;
    }

    function renderCard(shot, index) {
        const card = element("section", "h3c-card");
        card.dataset.index = String(index);
        card.style.setProperty("--h3c-scene-color", sceneColor(node, shot, index));
        card.addEventListener("dragover", (event) => {
            event.preventDefault();
            card.classList.add("h3c-drag-over");
        });
        card.addEventListener("dragleave", () => card.classList.remove("h3c-drag-over"));
        card.addEventListener("drop", (event) => {
            event.preventDefault();
            card.classList.remove("h3c-drag-over");
            if (state.draggedIndex === null || state.draggedIndex === index) return;
            moveShot(state.plan.shots, state.draggedIndex, index);
            state.draggedIndex = null;
            syncPlan();
            render();
        });

        const head = element("div", "h3c-card-head");
        const drag = element("span", "h3c-drag", "⠿");
        drag.title = "Drag to reorder";
        drag.draggable = true;
        drag.addEventListener("dragstart", (event) => {
            state.draggedIndex = index;
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", String(index));
        });
        drag.addEventListener("dragend", () => {
            state.draggedIndex = null;
            root.querySelectorAll(".h3c-drag-over").forEach((item) => item.classList.remove("h3c-drag-over"));
        });
        const ordinal = element("span", "h3c-index", `Scene ${index + 1}`);
        const color = element("input", "h3c-color");
        color.type = "color";
        color.value = sceneColor(node, shot, index);
        color.title = "Scene border color. Double-click to restore the automatic color.";
        color.setAttribute("aria-label", `Scene ${index + 1} border color`);
        color.addEventListener("input", () => {
            saveSceneColor(sceneColorKey(shot, index), color.value);
            card.style.setProperty("--h3c-scene-color", color.value);
        });
        color.addEventListener("dblclick", (event) => {
            event.preventDefault();
            saveSceneColor(sceneColorKey(shot, index), null);
            color.value = automaticSceneColor(index);
            card.style.setProperty("--h3c-scene-color", color.value);
        });
        const id = element("input", "h3c-id");
        id.type = "text";
        id.placeholder = `clip_${String(index + 1).padStart(4, "0")}`;
        id.value = shot.id ?? "";
        id.title = "Stable scene ID used in checkpoint filenames and resume validation. Keep it unique and avoid changing it after rendering.";
        id.addEventListener("input", () => {
            const previousKey = sceneColorKey(shot, index);
            if (id.value) shot.id = id.value;
            else delete shot.id;
            const nextKey = sceneColorKey(shot, index);
            const colors = colorOverrides(node);
            if (previousKey !== nextKey && colors[previousKey]) {
                colors[nextKey] = colors[previousKey];
                delete colors[previousKey];
                node.properties[SCENE_COLOR_PROPERTY] = {...colors};
            }
            const heights = planLayout(node).promptHeights;
            const previousPromptKey = `scene:${previousKey}`;
            const nextPromptKey = `scene:${nextKey}`;
            if (previousPromptKey !== nextPromptKey && heights[previousPromptKey]) {
                heights[nextPromptKey] = heights[previousPromptKey];
                delete heights[previousPromptKey];
            }
            syncPlan();
            void refreshSeedStatus();
        });
        const timingLabel = element("span", "h3c-timing");
        const up = button("↑", "Move scene up", () => {
            moveShot(state.plan.shots, index, index - 1);
            syncPlan();
            render();
        });
        up.disabled = index === 0;
        const down = button("↓", "Move scene down", () => {
            moveShot(state.plan.shots, index, index + 1);
            syncPlan();
            render();
        });
        down.disabled = index === state.plan.shots.length - 1;
        const copy = button("Duplicate", "Duplicate this scene", () => {
            if (state.plan.shots.length >= MAX_SHOTS) return;
            duplicateShot(state.plan.shots, index);
            syncPlan();
            render();
        });
        const remove = button("Delete", "Delete this scene", () => {
            if (state.plan.shots.length <= 1) return;
            if (!window.confirm(`Delete scene ${index + 1}?`)) return;
            saveSceneColor(sceneColorKey(shot, index), null);
            state.plan.shots.splice(index, 1);
            syncPlan();
            render();
        });
        remove.disabled = state.plan.shots.length <= 1;
        head.append(drag, color, ordinal, id, timingLabel, up, down, copy, remove);

        const lengthRow = element("div", "h3c-length-row");
        const mode = element("select");
        mode.title = "Choose whether this scene inherits the plan duration, requests seconds that are rounded up, or specifies an exact H3-valid frame count.";
        for (const [value, label] of [["default", "Plan default"], ["seconds", "Seconds"], ["frames", "Exact frames"]]) {
            const option = element("option", "", label);
            option.value = value;
            mode.append(option);
        }
        mode.value = lengthMode(shot);
        const value = numberInput("", {min: "0.01", step: "0.01"});
        const lengthHelp = element("span", "h3c-help");
        function refreshLengthControl() {
            const selected = lengthMode(shot);
            mode.value = selected;
            value.disabled = selected === "default";
            if (selected === "seconds") {
                value.min = "0.01";
                value.max = String(3592 / 24);
                value.step = "0.01";
                value.value = shot.duration_seconds ?? "";
                lengthHelp.textContent = "Rounded up to 17k+5.";
                value.title = "Requested seconds. The backend rounds up to the next H3-valid 17k+5 frame length at 24 fps.";
            } else if (selected === "frames") {
                value.min = "5";
                value.max = "3592";
                value.step = "17";
                value.value = shot.length ?? shot.frames ?? "";
                lengthHelp.textContent = "Must satisfy length % 17 = 5.";
                value.title = "Exact raw generation frames. Valid values satisfy frames % 17 = 5 and range from 5 to 3592.";
            } else {
                value.value = "";
                lengthHelp.textContent = "Uses JSON defaults, then node defaults.";
                value.title = "Disabled because this scene inherits the JSON default duration, then the Plan node default.";
            }
        }
        mode.addEventListener("change", () => {
            const fallback = state.plan.defaults?.duration_seconds
                ?? widgetValue(node, "default_duration_seconds", 15);
            updateShotLengthMode(shot, mode.value, fallback);
            refreshLengthControl();
            syncPlan();
        });
        value.addEventListener("input", () => {
            if (!value.value) return;
            if (mode.value === "seconds") shot.duration_seconds = Number(value.value);
            if (mode.value === "frames") {
                shot.length = Number(value.value);
                delete shot.frames;
            }
            syncPlan();
        });
        refreshLengthControl();
        lengthRow.append(mode, value, lengthHelp);

        const prompt = element("textarea", "h3c-prompt");
        prompt.value = promptValueToText(shot.prompt, `Scene ${index + 1} prompt`);
        prompt.placeholder = "Optional with a shared prompt; otherwise describe this scene…";
        prompt.title = "Scene-specific action, camera, performance, dialogue, and ending continuity. The shared prompt is automatically prepended; at least one of the two must contain text.";
        prompt.spellcheck = true;
        prompt.addEventListener("input", () => {
            shot.prompt = promptTextToLines(prompt.value);
            syncPlan();
        });
        bindTextareaHeight(
            prompt, `scene:${sceneColorKey(shot, index)}`, 112,
        );

        const seedControl = element("div", "h3c-seed-control");
        const seed = element("input");
        seed.type = "text";
        seed.inputMode = "numeric";
        seed.placeholder = "Blank = stable derived seed";
        seed.value = shot.seed ?? "";
        seed.title = "Explicit unsigned 64-bit seed for this scene. Leave blank to use the displayed stable seed derived from base_seed, scene index, and scene ID.";
        const seedStatus = element("span", "h3c-seed-status");
        async function refreshSeedStatus() {
            const explicit = seed.value.trim();
            useDerivedSeed.disabled = !explicit;
            if (explicit) {
                seedStatus.textContent = `Explicit seed: ${explicit}`;
                seedStatus.title = "This exact seed is stored in plan_json.";
                return;
            }
            const baseSeed = widgetValue(node, "base_seed", 0);
            const shotId = safeShotId(
                shot.id, `clip_${String(index + 1).padStart(4, "0")}`,
            );
            const request = `${baseSeed}:${index + 1}:${shotId}`;
            seedStatus.dataset.request = request;
            seedStatus.textContent = "Resolving stable derived seed…";
            try {
                const resolved = await derivedSceneSeed(baseSeed, index + 1, shotId);
                if (seedStatus.isConnected && !seed.value.trim()
                        && seedStatus.dataset.request === request) {
                    seedStatus.textContent = `Derived seed: ${resolved}`;
                    seedStatus.title = `Stable result of base_seed ${baseSeed}, scene ${index + 1}, and ID ${shotId}. It will repeat until one of those values changes.`;
                }
            } catch (error) {
                seedStatus.textContent = error.message;
            }
        }
        seed.addEventListener("input", () => {
            if (seed.value.trim()) shot.seed = seed.value.trim();
            else delete shot.seed;
            syncPlan();
            void refreshSeedStatus();
        });
        const randomizeSeed = button("New random", "Create and store a new explicit uint64 seed for only this scene", () => {
            seed.value = randomSceneSeed();
            shot.seed = seed.value;
            syncPlan();
            void refreshSeedStatus();
        });
        const useDerivedSeed = button("Use derived", "Remove this scene's explicit override and return to its stable base_seed-derived value", () => {
            seed.value = "";
            delete shot.seed;
            syncPlan();
            void refreshSeedStatus();
        });
        useDerivedSeed.disabled = !seed.value.trim();
        seedControl.append(seed, randomizeSeed, useDerivedSeed, seedStatus);
        state.seedRefreshers.push(refreshSeedStatus);
        void refreshSeedStatus();

        const advanced = element("div", "h3c-advanced-fields");
        const steps = numberInput(shot.steps ?? "", {min: "1", max: "10000", step: "1"});
        steps.placeholder = String(state.plan.defaults?.steps ?? widgetValue(node, "default_steps", 20));
        steps.title = "Optional sampler-step override for only this scene. Leave blank to inherit the JSON default, then the Plan node default.";
        steps.addEventListener("input", () => {
            if (steps.value) shot.steps = Number(steps.value);
            else delete shot.steps;
            syncPlan();
        });
        advanced.append(field("Steps (blank = default)", steps));
        card.append(
            head,
            lengthRow,
            field("Scene prompt (optional with shared prompt)", prompt),
            promptTools(prompt, index + 1),
            field("Scene seed", seedControl),
            advanced,
        );
        return card;
    }

    function render() {
        if (!state.plan) return;
        disconnectResizeObservers();
        state.seedRefreshers = [];
        const scrollTop = root.scrollTop;
        root.replaceChildren();
        root.classList.toggle("h3c-show-advanced", state.advanced);

        const header = element("div", "h3c-header");
        const openOutput = button(
            "Output",
            "Open this Plan's output/h3_chains/<run_name> folder on the ComfyUI host. " +
            "If the host has no desktop session, its path is copied instead.",
            async () => {
                const runName = String(widgetValue(node, "run_name", "")).trim();
                openOutput.disabled = true;
                setOutputButtonLabel(openOutput, "Opening…");
                try {
                    const response = await api.fetchApi(
                        "/minimax_h3_context_loop/open-run-folder",
                        {
                            method: "POST",
                            headers: {"Content-Type": "application/json"},
                            body: JSON.stringify({run_name: runName}),
                        },
                    );
                    const payload = await response.json();
                    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
                    openOutput.title = payload.path;
                    if (payload.opened) {
                        setOutputButtonLabel(openOutput, "Opened ✓");
                    } else {
                        try {
                            await navigator.clipboard.writeText(payload.path);
                            setOutputButtonLabel(openOutput, "Path copied");
                        } catch (_error) {
                            setOutputButtonLabel(openOutput, "See tooltip");
                        }
                        if (payload.error) openOutput.title += `\n${payload.error}`;
                    }
                } catch (error) {
                    setOutputButtonLabel(openOutput, "Open failed");
                    openOutput.title = String(error?.message || error);
                } finally {
                    setTimeout(() => {
                        setOutputButtonLabel(openOutput, "Output", true);
                        openOutput.disabled = false;
                    }, 2200);
                }
            },
        );
        openOutput.classList.add("h3c-open-output");
        openOutput.setAttribute("aria-label", "Open project output folder");
        setOutputButtonLabel(openOutput, "Output", true);
        const headerActions = element("div", "h3c-header-actions");
        headerActions.append(openOutput, element("div", "h3c-summary"));
        header.append(element("div", "h3c-title", "MiniMax H3 Scene Plan"), headerActions);

        const prefix = element("textarea", "h3c-prefix");
        prefix.value = sharedPrompt(state.plan).text;
        prefix.placeholder = "Identity, wardrobe, style and continuity rules shared by every scene…";
        prefix.title = "Text automatically prepended to every scene prompt. Put identity, wardrobe, reference definitions, audio rules, style, and global continuity here instead of repeating them.";
        prefix.spellcheck = true;
        prefix.addEventListener("input", () => {
            setSharedPrompt(state.plan, prefix.value);
            syncPlan();
        });
        bindTextareaHeight(prefix, "shared", 88);
        const prefixSection = element("section", "h3c-section");
        prefixSection.append(
            field("Shared prompt — automatically prepended to every scene", prefix),
            promptTools(prefix, null),
        );

        state.plan.defaults = state.plan.defaults
            && typeof state.plan.defaults === "object"
            && !Array.isArray(state.plan.defaults)
            ? state.plan.defaults : {};
        const defaultDuration = numberInput(state.plan.defaults.duration_seconds ?? "", {
            min: "0.01", max: String(3592 / 24), step: "0.01",
        });
        defaultDuration.placeholder = String(widgetValue(node, "default_duration_seconds", 15));
        defaultDuration.title = "Optional JSON-level default duration for scenes that use Plan default. Leave blank to use the Plan node's default_duration_seconds widget.";
        defaultDuration.addEventListener("input", () => {
            if (defaultDuration.value) state.plan.defaults.duration_seconds = Number(defaultDuration.value);
            else delete state.plan.defaults.duration_seconds;
            syncPlan();
        });
        const defaultSteps = numberInput(state.plan.defaults.steps ?? "", {min: "1", max: "10000", step: "1"});
        defaultSteps.placeholder = String(widgetValue(node, "default_steps", 20));
        defaultSteps.title = "Optional JSON-level sampler-step default. Leave blank to use the Plan node's default_steps widget.";
        defaultSteps.addEventListener("input", () => {
            if (defaultSteps.value) state.plan.defaults.steps = Number(defaultSteps.value);
            else delete state.plan.defaults.steps;
            syncPlan();
        });
        const defaults = element("section", "h3c-section h3c-defaults");
        defaults.append(
            element("div", "h3c-help", "Scene values override these plan defaults. Blank fields use the Plan node widgets above."),
            field("Default seconds", defaultDuration),
            field("Default steps", defaultSteps),
        );

        const toolbar = element("div", "h3c-toolbar");
        const add = button("+ Add scene", "Append a new scene", () => {
            if (state.plan.shots.length >= MAX_SHOTS) return;
            state.plan.shots.push(makeShot(state.plan.shots));
            syncPlan();
            render();
        });
        add.disabled = state.plan.shots.length >= MAX_SHOTS;
        const advanced = button(state.advanced ? "Hide steps" : "Show steps", "Show or hide per-scene sampler-step overrides", () => {
            state.advanced = !state.advanced;
            savePanelState();
            render();
        });
        const json = button(state.jsonOpen ? "Hide raw JSON" : "Raw JSON", "Expand the raw plan JSON for direct editing, import, or export", () => {
            state.jsonOpen = !state.jsonOpen;
            savePanelState();
            render();
        });
        toolbar.append(add, advanced, element("span", "h3c-spacer"), json);

        const errors = element("div", "h3c-errors");
        const cards = element("div", "h3c-cards");
        state.plan.shots.forEach((shot, index) => cards.append(renderCard(shot, index)));

        const jsonPanel = element("section", `h3c-section h3c-json-panel${state.jsonOpen ? " h3c-open" : ""}`);
        const jsonArea = element("textarea", "h3c-json");
        jsonArea.value = planToJson(state.plan);
        jsonArea.spellcheck = false;
        jsonArea.title = "Canonical plan JSON. Apply JSON replaces the visual editor contents after validation; saving the workflow also serializes this value.";
        bindTextareaHeight(jsonArea, "json", 260);
        const jsonStatus = element("span", "h3c-json-status", "Raw JSON escape hatch");
        const apply = button("Apply JSON", "Validate and load this JSON into the scene editor", () => {
            try {
                state.plan = parsePlanJson(jsonArea.value);
                jsonStatus.textContent = "JSON applied";
                syncPlan();
                render();
            } catch (error) {
                jsonStatus.textContent = error.message;
            }
        });
        const copyJson = button("Copy", "Copy plan JSON", async () => {
            try {
                await navigator.clipboard.writeText(jsonArea.value);
                jsonStatus.textContent = "Copied";
            } catch (_error) {
                jsonArea.select();
                document.execCommand("copy");
                jsonStatus.textContent = "Copied";
            }
        });
        const saveJson = button("Save .json", "Download this plan as JSON", () => {
            const runName = String(widgetValue(node, "run_name", "h3_chain"));
            downloadJson(jsonArea.value, `${runName || "h3_chain"}_plan.json`);
        });
        const loadInput = element("input");
        loadInput.type = "file";
        loadInput.accept = ".json,application/json";
        loadInput.hidden = true;
        loadInput.addEventListener("change", async () => {
            const file = loadInput.files?.[0];
            if (!file) return;
            jsonArea.value = await file.text();
            apply.click();
            loadInput.value = "";
        });
        const loadJson = button("Load .json", "Load a plan JSON file", () => loadInput.click());
        const jsonActions = element("div", "h3c-json-actions");
        jsonActions.append(apply, copyJson, saveJson, loadJson, loadInput, jsonStatus);
        jsonPanel.append(jsonArea, jsonActions);

        const footer = element("div", "h3c-footer");
        footer.append(
            element("span", "", "Edits are serialized into plan_json; the backend contract is unchanged."),
            Object.assign(element("a", "", "UI inspiration: nkxx188"), {
                href: "https://github.com/nkxx188/ComfyUI-MiniMaxH3-Easy",
                target: "_blank",
                rel: "noreferrer",
            }),
        );

        root.append(header, prefixSection, defaults, toolbar, errors, cards, jsonPanel, footer);
        updateTiming();
        requestAnimationFrame(() => { root.scrollTop = scrollTop; });
        graphDirty();
    }

    function loadFromWidget(force = false) {
        const value = String(planWidget.value ?? "");
        if (!force && value === state.lastWidgetValue) return;
        try {
            state.plan = parsePlanJson(value);
            state.lastWidgetValue = value;
            render();
        } catch (error) {
            root.replaceChildren();
            const failure = element("div", "h3c-errors h3c-open", error.message);
            const raw = element("textarea", "h3c-json");
            raw.value = value;
            const retry = button("Apply repaired JSON", "Parse this JSON and open the scene editor", () => {
                planWidget.value = raw.value;
                state.lastWidgetValue = "";
                loadFromWidget(true);
            });
            root.append(failure, raw, retry);
        }
    }

    const originalCallback = planWidget.callback;
    planWidget.callback = function (...args) {
        const result = originalCallback?.apply(this, args);
        if (!state.syncing) setTimeout(() => loadFromWidget(), 0);
        return result;
    };

    for (const name of [
        "context_length", "anchor_mode", "default_duration_seconds",
        "default_steps", "base_seed",
    ]) {
        const widget = node.widgets?.find((item) => item.name === name);
        if (!widget || widget._h3TimingWrapped) continue;
        const callback = widget.callback;
        widget.callback = function (...args) {
            const result = callback?.apply(this, args);
            setTimeout(() => updateTiming(), 0);
            if (name === "base_seed") {
                setTimeout(() => {
                    for (const refresh of state.seedRefreshers) void refresh();
                }, 0);
            }
            return result;
        };
        widget._h3TimingWrapped = true;
    }

    node._h3ChainEditorRefresh = () => {
        collapseWidget(planWidget);
        const layout = planLayout(node);
        state.advanced = Boolean(layout.advanced);
        state.jsonOpen = Boolean(layout.jsonOpen);
        loadFromWidget(true);
        scheduleResponsiveSize();
    };
    node._h3ChainEditorFit = applyResponsiveSize;
    loadFromWidget(true);
    scheduleResponsiveSize();
    const removed = node.onRemoved;
    node.onRemoved = function () {
        disconnectResizeObservers();
        return removed?.apply(this, arguments);
    };
}

app.registerExtension({
    name: EXTENSION,
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_NAME) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = onNodeCreated?.apply(this, arguments);
            setTimeout(() => mountEditor(this), 0);
            return result;
        };

        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const result = onConfigure?.apply(this, arguments);
            setTimeout(() => this._h3ChainEditorRefresh?.(), 0);
            return result;
        };

        const onGraphConfigured = nodeType.prototype.onGraphConfigured;
        nodeType.prototype.onGraphConfigured = function () {
            const result = onGraphConfigured?.apply(this, arguments);
            setTimeout(() => this._h3ChainEditorRefresh?.(), 0);
            return result;
        };
    },
});
