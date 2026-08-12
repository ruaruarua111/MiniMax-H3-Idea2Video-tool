export const CORE_REF2VA_TYPE = "MiniMaxH3ReferenceToVideo";
export const SCHEDULED_REF2VA_TYPE = "MiniMaxH3ScheduledReferenceToVideo";
export const PICTURE_REF_TYPE = "MiniMaxH3ScheduledPictureReference";
export const VIDEO_REF_TYPE = "MiniMaxH3ScheduledVideoReference";
export const AUDIO_REF_TYPE = "MiniMaxH3ScheduledAudioReference";
export const CURRENT_SHOT_TYPE = "MiniMaxH3ChainCurrent";
export const PLAN_TYPE = "MiniMaxH3ChainPlan";

const COMMON_INPUTS = [
    "clip", "vae", "audio_vae", "prompt", "width", "height", "length",
    "ref_image_size",
];

export function nodeType(node) {
    return node?.comfyClass ?? node?.type ?? null;
}

function inputSlot(node, name) {
    return node?.inputs?.findIndex((input) => input.name === name) ?? -1;
}

function outputSlot(node, name, fallback = -1) {
    const index = node?.outputs?.findIndex((output) => output.name === name) ?? -1;
    return index >= 0 ? index : fallback;
}

function widget(node, name) {
    return node?.widgets?.find((item) => item.name === name) ?? null;
}

function inputSource(node, name, graph) {
    const slot = inputSlot(node, name);
    if (slot < 0) return null;
    const linkId = node.inputs[slot]?.link;
    if (linkId == null) return null;
    const link = graph?.links?.[linkId];
    if (!link) return null;
    const source = graph.getNodeById?.(link.origin_id);
    return source ? {node: source, slot: Number(link.origin_slot), link} : null;
}

function connectedTargets(node, slot, graph) {
    const links = [...(node?.outputs?.[slot]?.links ?? [])];
    return links.flatMap((linkId) => {
        const link = graph?.links?.[linkId];
        const target = link ? graph.getNodeById?.(link.target_id) : null;
        return target ? [{node: target, slot: Number(link.target_slot)}] : [];
    });
}

function connect(source, sourceSlot, target, targetName) {
    const targetSlot = inputSlot(target, targetName);
    if (targetSlot < 0) {
        throw new Error(`${nodeType(target)} has no ${targetName} input.`);
    }
    source.connect(sourceSlot, target, targetSlot);
}

function setWidget(node, name, value) {
    const target = widget(node, name);
    if (!target) return false;
    target.value = value;
    target.callback?.(value);
    return true;
}

export function migrateLegacyVideoScheduleWidgets(node, info) {
    const values = info?.widgets_values;
    if (nodeType(node) !== VIDEO_REF_TYPE || !Array.isArray(values)
            || values.length < 4) return false;
    // v0.3.10 stored [tag, scenes, declaration, audio_tag,
    // audio_declaration]. The lean scheduler stores [tag, scenes, audio_tag].
    return setWidget(node, "audio_tag", values[3] ?? "");
}

function copyWidget(oldNode, newNode, name) {
    const source = widget(oldNode, name);
    const target = widget(newNode, name);
    if (!source || !target) return;
    target.value = source.value;
    target.callback?.(target.value);
}

function numberedInput(name, pattern) {
    const match = pattern.exec(String(name));
    return match ? Number(match[1]) : null;
}

export function collectReferenceInputs(node, graph = node?.graph) {
    const pictures = [];
    const videos = new Map();
    const standaloneAudios = [];
    const orphanVideoAudios = [];

    for (const input of node?.inputs ?? []) {
        const source = inputSource(node, input.name, graph);
        if (!source) continue;
        let index = numberedInput(input.name, /^ref_images\.ref_image_(\d+)$/);
        if (index != null) {
            pictures.push({kind: "picture", index, source});
            continue;
        }
        index = numberedInput(input.name, /^ref_videos\.ref_video_(\d+)$/);
        if (index != null) {
            const entry = videos.get(index) ?? {kind: "video", index};
            entry.source = source;
            videos.set(index, entry);
            continue;
        }
        index = numberedInput(
            input.name, /^ref_video_audios\.ref_video_audio_(\d+)$/);
        if (index != null) {
            const entry = videos.get(index) ?? {kind: "video", index};
            entry.audioSource = source;
            videos.set(index, entry);
            continue;
        }
        index = numberedInput(input.name, /^ref_audios\.ref_audio_(\d+)$/);
        if (index != null) {
            standaloneAudios.push({kind: "audio", index, source});
        }
    }

    const validVideos = [];
    for (const entry of videos.values()) {
        if (entry.source) validVideos.push(entry);
        else if (entry.audioSource) orphanVideoAudios.push(entry);
    }
    pictures.sort((left, right) => left.index - right.index);
    validVideos.sort((left, right) => left.index - right.index);
    standaloneAudios.sort((left, right) => left.index - right.index);
    return {
        entries: [...pictures, ...validVideos, ...standaloneAudios],
        orphanVideoAudios,
    };
}

function hasUpstreamType(start, type, graph) {
    const queue = [start];
    const seen = new Set();
    while (queue.length) {
        const node = queue.shift();
        if (!node || seen.has(node)) continue;
        seen.add(node);
        if (nodeType(node) === type) return true;
        for (const input of node.inputs ?? []) {
            const link = input.link == null ? null : graph?.links?.[input.link];
            const parent = link ? graph.getNodeById?.(link.origin_id) : null;
            if (parent) queue.push(parent);
        }
    }
    return false;
}

function findCurrentShot(oldNode, graph) {
    const linked = COMMON_INPUTS.flatMap((name) => {
        const source = inputSource(oldNode, name, graph)?.node;
        return source && nodeType(source) === CURRENT_SHOT_TYPE ? [source] : [];
    });
    if (linked.length) return linked[0];
    const candidates = (graph?._nodes ?? []).filter(
        (node) => nodeType(node) === CURRENT_SHOT_TYPE);
    return candidates.length === 1 ? candidates[0] : null;
}

function addNode(graph, createNode, type, created) {
    const node = createNode(type);
    if (!node) throw new Error(`Could not create ${type}; restart ComfyUI first.`);
    graph.add(node);
    created.push(node);
    return node;
}

function scheduleNodeFor(entry, graph, createNode, created) {
    const types = {
        picture: PICTURE_REF_TYPE,
        video: VIDEO_REF_TYPE,
        audio: AUDIO_REF_TYPE,
    };
    const node = addNode(graph, createNode, types[entry.kind], created);
    const ordinal = entry.index + 1;
    if (entry.kind === "picture") {
        setWidget(node, "tag", `picture_${ordinal}`);
        connect(entry.source.node, entry.source.slot, node, "image");
    } else if (entry.kind === "video") {
        setWidget(node, "tag", `video_${ordinal}`);
        setWidget(node, "audio_tag", "");
        connect(entry.source.node, entry.source.slot, node, "video");
        if (entry.audioSource) {
            connect(entry.audioSource.node, entry.audioSource.slot, node, "audio");
        }
    } else {
        setWidget(node, "tag", `audio_${ordinal}`);
        connect(entry.source.node, entry.source.slot, node, "audio");
    }
    setWidget(node, "scenes", "");
    return node;
}

function transferInputs(oldNode, wrapper, graph) {
    for (const name of COMMON_INPUTS) {
        const source = inputSource(oldNode, name, graph);
        if (source) connect(source.node, source.slot, wrapper, name);
        else copyWidget(oldNode, wrapper, name);
    }
}

function transferOutputs(oldNode, wrapper, graph) {
    for (let slot = 0; slot < 2; slot += 1) {
        const targets = connectedTargets(oldNode, slot, graph);
        for (const target of targets) wrapper.connect(slot, target.node, target.slot);
    }
}

function connectCurrentShot(current, wrapper) {
    if (!current) return false;
    for (const name of ["clip_index", "clip_count"]) {
        const slot = outputSlot(current, name);
        if (slot >= 0 && inputSlot(wrapper, name) >= 0) {
            connect(current, slot, wrapper, name);
        }
    }
    return true;
}

function connectStaticFingerprint(finalSchedule, entries, graph) {
    if (entries.some((entry) => {
        const sources = [entry.source?.node, entry.audioSource?.node].filter(Boolean);
        return sources.some((source) => hasUpstreamType(
            source, CURRENT_SHOT_TYPE, graph));
    })) return false;
    const plans = (graph?._nodes ?? []).filter((node) => nodeType(node) === PLAN_TYPE);
    if (plans.length !== 1) return false;
    const plan = plans[0];
    const targetSlot = inputSlot(plan, "generation_fingerprint");
    const fingerprintSlot = outputSlot(finalSchedule, "schedule_fingerprint", 1);
    const existingValue = String(
        widget(plan, "generation_fingerprint")?.value ?? "").trim();
    if (targetSlot < 0 || fingerprintSlot < 0 || plan.inputs[targetSlot]?.link != null) {
        return false;
    }
    // A user-supplied model/LoRA fingerprint must not be silently replaced.
    // Leave the schedule hash visible so it can be combined explicitly.
    if (existingValue) return false;
    finalSchedule.connect(fingerprintSlot, plan, targetSlot);
    return true;
}

export function convertCoreRef2VA(oldNode, {createNode} = {}) {
    const graph = oldNode?.graph;
    if (!graph) throw new Error("The Ref2VA node is not attached to a graph.");
    if (nodeType(oldNode) !== CORE_REF2VA_TYPE) {
        throw new Error("Select a core MiniMax H3 Reference to Video node.");
    }
    if (typeof createNode !== "function") {
        throw new Error("The ComfyUI node factory is unavailable.");
    }
    const references = collectReferenceInputs(oldNode, graph);
    if (!references.entries.length) {
        throw new Error(
            "Connect at least one picture, video, or audio reference before converting.");
    }

    const created = [];
    graph.beforeChange?.();
    try {
        const wrapper = addNode(
            graph, createNode, SCHEDULED_REF2VA_TYPE, created);
        wrapper.pos = [...(oldNode.pos ?? [0, 0])];
        wrapper.size = [
            Math.max(oldNode.size?.[0] ?? 320, wrapper.size?.[0] ?? 320),
            wrapper.size?.[1] ?? oldNode.size?.[1] ?? 240,
        ];
        wrapper.mode = oldNode.mode;
        transferInputs(oldNode, wrapper, graph);

        let previous = null;
        const scheduleNodes = [];
        references.entries.forEach((entry, index) => {
            const node = scheduleNodeFor(entry, graph, createNode, created);
            node.pos = [
                (oldNode.pos?.[0] ?? 0) - 380,
                (oldNode.pos?.[1] ?? 0) + index * 190,
            ];
            if (previous) connect(previous, 0, node, "previous");
            previous = node;
            scheduleNodes.push(node);
        });
        connect(previous, 0, wrapper, "reference_schedule");

        const current = findCurrentShot(oldNode, graph);
        const connectedCurrent = connectCurrentShot(current, wrapper);
        const connectedFingerprint = connectStaticFingerprint(
            previous, references.entries, graph);

        transferOutputs(oldNode, wrapper, graph);
        graph.remove(oldNode);
        graph.afterChange?.();
        graph.setDirtyCanvas?.(true, true);
        return {
            wrapper,
            scheduleNodes,
            connectedCurrent,
            connectedFingerprint,
            ignoredOrphanVideoAudio: references.orphanVideoAudios.length,
        };
    } catch (error) {
        for (const node of created.reverse()) {
            if (node.graph === graph) graph.remove(node);
        }
        graph.afterChange?.();
        throw error;
    }
}
