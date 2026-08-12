export const SCHEDULED_REF2VA_TYPE = "MiniMaxH3ScheduledReferenceToVideo";
export const CORE_REF2VA_TYPE = "MiniMaxH3ReferenceToVideo";
export const IMAGE_TO_VIDEO_TYPE = "MiniMaxH3ImageToVideo";
export const FIRST_SCENE_IMAGE_TYPE = "MiniMaxH3ChainFirstSceneImage";
export const PICTURE_REF_TYPE = "MiniMaxH3ScheduledPictureReference";
export const VIDEO_REF_TYPE = "MiniMaxH3ScheduledVideoReference";
export const AUDIO_REF_TYPE = "MiniMaxH3ScheduledAudioReference";

const SCHEDULE_TYPES = new Set([
    PICTURE_REF_TYPE,
    VIDEO_REF_TYPE,
    AUDIO_REF_TYPE,
]);

export function nodeType(node) {
    return node?.comfyClass ?? node?.type ?? null;
}

function widgetValue(node, name, fallback = "") {
    return node?.widgets?.find((item) => item.name === name)?.value ?? fallback;
}

export function referenceTag(value) {
    return String(value ?? "").trim().replace(/^@+/, "");
}

function inputSource(node, name) {
    const input = node?.inputs?.find((item) => item.name === name);
    const link = input?.link == null ? null : node.graph?.links?.[input.link];
    return link ? node.graph?.getNodeById?.(link.origin_id) ?? null : null;
}

function outputTargets(node) {
    const targets = [];
    for (const output of node?.outputs ?? []) {
        for (const linkId of output.links ?? []) {
            const link = node.graph?.links?.[linkId];
            const target = link ? node.graph?.getNodeById?.(link.target_id) : null;
            if (target) targets.push(target);
        }
    }
    return targets;
}

function findDownstreamType(start, wantedType) {
    const queue = [start];
    const seen = new Set();
    while (queue.length) {
        const node = queue.shift();
        if (!node || seen.has(node)) continue;
        seen.add(node);
        if (node !== start && nodeType(node) === wantedType) return node;
        queue.push(...outputTargets(node));
    }
    return null;
}

export function findScheduledRef2VA(start) {
    return findDownstreamType(start, SCHEDULED_REF2VA_TYPE);
}

export function findCoreRef2VA(start) {
    return findDownstreamType(start, CORE_REF2VA_TYPE);
}

export function findImageToVideo(start) {
    return findDownstreamType(start, IMAGE_TO_VIDEO_TYPE);
}

export function collectScheduleNodes(wrapper) {
    const result = [];
    const seen = new Set();
    let current = inputSource(wrapper, "reference_schedule");
    while (current && SCHEDULE_TYPES.has(nodeType(current)) && !seen.has(current)) {
        seen.add(current);
        result.unshift(current);
        current = inputSource(current, "previous");
    }
    return result;
}

export function referenceIsActive(selector, scene) {
    const text = String(selector ?? "").trim().toLowerCase();
    if (!text || text === "all" || text === "*") return true;
    const target = Number(scene);
    if (!Number.isInteger(target) || target < 1) return false;
    return text.split(",").some((piece) => {
        const match = piece.trim().match(/^(\d+)(?::(\d+))?$/);
        if (!match) return false;
        const first = Number(match[1]);
        const last = Number(match[2] ?? match[1]);
        return first <= target && target <= last;
    });
}

function baseRecord(node, kind, scene, inputName, tagName = "tag") {
    const selector = String(widgetValue(node, "scenes", ""));
    return {
        node,
        kind,
        tag: referenceTag(widgetValue(node, tagName, "")),
        selector: selector.trim() || "all",
        active: referenceIsActive(selector, scene),
        source: inputSource(node, inputName),
        label: null,
    };
}

export function scheduledReferenceRecords(editorNode, scene) {
    const wrapper = findScheduledRef2VA(editorNode);
    if (!wrapper) return {wrapper: null, records: []};
    const nodes = collectScheduleNodes(wrapper);
    const pictures = [];
    const videos = [];
    const pairedAudios = [];
    const audios = [];

    for (const node of nodes) {
        const type = nodeType(node);
        if (type === PICTURE_REF_TYPE) {
            pictures.push(baseRecord(node, "picture", scene, "image"));
        } else if (type === VIDEO_REF_TYPE) {
            const video = baseRecord(node, "video", scene, "video");
            videos.push(video);
            const audioSource = inputSource(node, "audio");
            if (audioSource) {
                const explicit = referenceTag(widgetValue(node, "audio_tag", ""));
                pairedAudios.push({
                    node,
                    kind: "audio",
                    tag: explicit || `${video.tag}_audio`,
                    selector: video.selector,
                    active: video.active,
                    source: audioSource,
                    label: null,
                    pairedWith: video,
                });
            }
        } else if (type === AUDIO_REF_TYPE) {
            audios.push(baseRecord(node, "audio", scene, "audio"));
        }
    }

    let ordinal = 0;
    for (const item of pictures) {
        if (item.active) item.label = `<Picture ${++ordinal}>`;
    }
    ordinal = 0;
    for (const item of videos) {
        if (item.active) item.label = `<Video ${++ordinal}>`;
    }
    ordinal = 0;
    // Core Ref2VA numbers paired video soundtracks before standalone audio.
    for (const item of pairedAudios) {
        if (item.active) item.label = `<Audio ${++ordinal}>`;
    }
    for (const item of audios) {
        if (item.active) item.label = `<Audio ${++ordinal}>`;
    }

    return {
        wrapper,
        mode: "scheduled",
        records: [...pictures, ...videos, ...pairedAudios, ...audios]
            .filter((item) => item.tag)
            .map((item) => ({...item, token: `@${item.tag}`})),
    };
}

function numberedInputRecords(wrapper, pattern, kind, labelKind) {
    const records = [];
    for (const input of wrapper?.inputs ?? []) {
        const match = String(input.name ?? "").match(pattern);
        if (!match || input.link == null) continue;
        const index = Number(match[1]);
        const label = `<${labelKind} ${index + 1}>`;
        records.push({
            node: wrapper,
            kind,
            tag: "",
            token: label,
            selector: "all",
            active: true,
            source: inputSource(wrapper, input.name),
            label,
            index,
            mode: "native",
        });
    }
    records.sort((left, right) => left.index - right.index);
    return records;
}

export function coreReferenceRecords(editorNode) {
    const wrapper = findCoreRef2VA(editorNode);
    if (!wrapper) return {wrapper: null, mode: null, records: []};
    const pictures = numberedInputRecords(
        wrapper, /^ref_images\.ref_image_(\d+)$/, "picture", "Picture");
    const videos = numberedInputRecords(
        wrapper, /^ref_videos\.ref_video_(\d+)$/, "video", "Video");
    const pairedAudios = numberedInputRecords(
        wrapper, /^ref_video_audios\.ref_video_audio_(\d+)$/, "audio", "Audio");
    const audios = numberedInputRecords(
        wrapper, /^ref_audios\.ref_audio_(\d+)$/, "audio", "Audio");
    // Audio labels are shared across video-paired and standalone references.
    [...pairedAudios, ...audios].forEach((item, index) => {
        item.label = `<Audio ${index + 1}>`;
        item.token = item.label;
    });
    return {
        wrapper,
        mode: "native",
        records: [...pictures, ...videos, ...pairedAudios, ...audios],
    };
}

export function imageToVideoReferenceRecords(editorNode, scene = 1) {
    const wrapper = findImageToVideo(editorNode);
    if (!wrapper) return {wrapper: null, mode: null, records: []};
    const firstFrame = inputSource(wrapper, "first_frame");
    const lastFrame = inputSource(wrapper, "last_frame");
    const records = [];
    if (firstFrame) {
        const firstSceneOnly = nodeType(firstFrame) === FIRST_SCENE_IMAGE_TYPE;
        const active = !firstSceneOnly || Number(scene) === 1;
        records.push({
            node: wrapper,
            kind: "picture",
            tag: "",
            token: "<Picture 1>",
            selector: firstSceneOnly ? "1" : "all",
            active,
            source: firstFrame,
            label: "<Picture 1>",
            mode: "native",
            role: "first frame",
        });
    }
    if (lastFrame) {
        const ordinal = firstFrame ? 2 : 1;
        records.push({
            node: wrapper,
            kind: "picture",
            tag: "",
            token: `<Picture ${ordinal}>`,
            selector: "all",
            active: true,
            source: lastFrame,
            label: `<Picture ${ordinal}>`,
            mode: "native",
            role: "last frame",
        });
    }
    return {wrapper, mode: "native_keyframes", records};
}

export function referencePreviewRecords(editorNode, scene) {
    const scheduled = scheduledReferenceRecords(editorNode, scene);
    if (scheduled.wrapper) return scheduled;
    const core = coreReferenceRecords(editorNode);
    if (core.wrapper) return core;
    return imageToVideoReferenceRecords(editorNode, scene);
}

export function availableReferenceRecords(
    editorNode, scene, {includeInactive = false} = {},
) {
    const result = referencePreviewRecords(editorNode, scene);
    return {
        ...result,
        records: result.records.filter(
            (record) => record.source && (includeInactive || record.active),
        ),
    };
}
