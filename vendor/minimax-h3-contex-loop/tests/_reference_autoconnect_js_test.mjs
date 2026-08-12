import assert from "node:assert/strict";
import fs from "node:fs";
import {
    AUDIO_REF_TYPE,
    CORE_REF2VA_TYPE,
    CURRENT_SHOT_TYPE,
    PICTURE_REF_TYPE,
    PLAN_TYPE,
    SCHEDULED_REF2VA_TYPE,
    VIDEO_REF_TYPE,
    collectReferenceInputs,
    convertCoreRef2VA,
    migrateLegacyVideoScheduleWidgets,
} from "../web/h3_reference_autoconnect_core.mjs";

class FakeNode {
    constructor(type, inputs = [], outputs = [], widgets = []) {
        this.comfyClass = type;
        this.type = type;
        this.inputs = inputs.map((name) => ({name, link: null}));
        this.outputs = outputs.map((name) => ({name, links: null}));
        this.widgets = widgets.map(([name, value]) => ({name, value}));
        this.pos = [0, 0];
        this.size = [320, 200];
        this.mode = 0;
    }

    connect(originSlot, target, targetSlot) {
        const graph = this.graph;
        const oldLinkId = target.inputs[targetSlot].link;
        if (oldLinkId != null) graph.removeLink(oldLinkId);
        const id = graph.nextLink++;
        graph.links[id] = {
            origin_id: this.id,
            origin_slot: originSlot,
            target_id: target.id,
            target_slot: targetSlot,
        };
        this.outputs[originSlot].links ??= [];
        this.outputs[originSlot].links.push(id);
        target.inputs[targetSlot].link = id;
        return id;
    }
}

class FakeGraph {
    constructor() {
        this._nodes = [];
        this.links = {};
        this.nextNode = 1;
        this.nextLink = 1;
        this.before = 0;
        this.after = 0;
    }

    add(node) {
        node.id ??= this.nextNode++;
        node.graph = this;
        this._nodes.push(node);
        return node;
    }

    getNodeById(id) {
        return this._nodes.find((node) => node.id === id) ?? null;
    }

    removeLink(id) {
        const link = this.links[id];
        if (!link) return;
        const source = this.getNodeById(link.origin_id);
        const target = this.getNodeById(link.target_id);
        if (source?.outputs?.[link.origin_slot]?.links) {
            source.outputs[link.origin_slot].links = source.outputs[
                link.origin_slot].links.filter((value) => value !== id);
        }
        if (target?.inputs?.[link.target_slot]?.link === id) {
            target.inputs[link.target_slot].link = null;
        }
        delete this.links[id];
    }

    remove(node) {
        for (const input of node.inputs ?? []) {
            if (input.link != null) this.removeLink(input.link);
        }
        for (const output of node.outputs ?? []) {
            for (const link of [...(output.links ?? [])]) this.removeLink(link);
        }
        this._nodes = this._nodes.filter((item) => item !== node);
        node.graph = null;
    }

    beforeChange() { this.before += 1; }
    afterChange() { this.after += 1; }
    setDirtyCanvas() {}
}

const commonInputs = [
    "clip", "vae", "audio_vae", "prompt", "width", "height", "length",
    "ref_image_size",
];

function schema(type) {
    if (type === SCHEDULED_REF2VA_TYPE) {
        return new FakeNode(type, [
            "clip", "vae", "audio_vae", "reference_schedule", "clip_index",
            "clip_count", "prompt", "width", "height", "length",
            "ref_image_size",
        ], ["positive", "latent", "compiled_prompt", "active_references",
            "schedule_fingerprint"], [
            ["prompt", ""], ["width", 960], ["height", 544], ["length", 124],
            ["ref_image_size", "match"],
        ]);
    }
    if (type === PICTURE_REF_TYPE) {
        return new FakeNode(type, ["image", "previous"], [
            "schedule", "schedule_fingerprint", "status"], [
            ["tag", "hero_face"], ["scenes", ""],
        ]);
    }
    if (type === VIDEO_REF_TYPE) {
        return new FakeNode(type, ["video", "audio", "previous"], [
            "schedule", "schedule_fingerprint", "status"], [
            ["tag", "performance"], ["scenes", ""], ["audio_tag", ""],
        ]);
    }
    if (type === AUDIO_REF_TYPE) {
        return new FakeNode(type, ["audio", "previous"], [
            "schedule", "schedule_fingerprint", "status"], [
            ["tag", "voice"], ["scenes", ""],
        ]);
    }
    return null;
}

function source(type, outputNames) {
    return new FakeNode(type, [], outputNames);
}

function connectNamed(sourceNode, sourceName, target, targetName) {
    const sourceSlot = sourceNode.outputs.findIndex((item) => item.name === sourceName);
    const targetSlot = target.inputs.findIndex((item) => item.name === targetName);
    assert.ok(sourceSlot >= 0 && targetSlot >= 0);
    sourceNode.connect(sourceSlot, target, targetSlot);
}

function makeCore(extraInputs = []) {
    return new FakeNode(CORE_REF2VA_TYPE, [...commonInputs, ...extraInputs], [
        "positive", "latent",
    ], [
        ["prompt", "original prompt"], ["width", 1280], ["height", 720],
        ["length", 362], ["ref_image_size", "max"],
    ]);
}

const graph = new FakeGraph();
const plan = graph.add(new FakeNode(
    PLAN_TYPE, ["generation_fingerprint"], ["plan"]));
const current = graph.add(source(CURRENT_SHOT_TYPE, [
    "state", "clip_index", "clip_count", "shot_id", "prompt", "noise_seed",
    "length", "steps", "width", "height", "audio_start", "audio_duration",
    "source_audio_slice", "status",
]));
const models = graph.add(source("Models", ["clip", "vae", "audio_vae"]));
const image = graph.add(source("LoadImage", ["IMAGE"]));
const video = graph.add(source("LoadVideoFrames", ["IMAGE"]));
const pairedAudio = graph.add(source("LoadVideoAudio", ["AUDIO"]));
const voice = graph.add(source("LoadAudio", ["AUDIO"]));
const core = graph.add(makeCore([
    "ref_images.ref_image_0",
    "ref_videos.ref_video_0",
    "ref_video_audios.ref_video_audio_0",
    "ref_audios.ref_audio_0",
]));
core.pos = [900, 300];
const positiveTarget = graph.add(new FakeNode("Context", ["positive"], []));
const latentTarget = graph.add(new FakeNode("Sampler", ["latent"], []));

connectNamed(models, "clip", core, "clip");
connectNamed(models, "vae", core, "vae");
connectNamed(models, "audio_vae", core, "audio_vae");
for (const name of ["prompt", "width", "height", "length"]) {
    connectNamed(current, name, core, name);
}
connectNamed(image, "IMAGE", core, "ref_images.ref_image_0");
connectNamed(video, "IMAGE", core, "ref_videos.ref_video_0");
connectNamed(pairedAudio, "AUDIO", core, "ref_video_audios.ref_video_audio_0");
connectNamed(voice, "AUDIO", core, "ref_audios.ref_audio_0");
core.connect(0, positiveTarget, 0);
core.connect(1, latentTarget, 0);

const collected = collectReferenceInputs(core, graph);
assert.deepEqual(collected.entries.map((entry) => entry.kind), [
    "picture", "video", "audio",
]);
assert.equal(collected.entries[1].audioSource.node, pairedAudio);

const converted = convertCoreRef2VA(core, {createNode: schema});
assert.equal(graph.getNodeById(core.id), null);
assert.equal(converted.scheduleNodes.length, 3);
assert.equal(converted.connectedCurrent, true);
assert.equal(converted.connectedFingerprint, true);
assert.equal(plan.inputs[0].link != null, true);
assert.equal(converted.wrapper.pos[0], 900);
assert.equal(converted.wrapper.pos[1], 300);
assert.equal(converted.wrapper.widgets.find((item) => item.name === "ref_image_size").value, "max");
assert.deepEqual(converted.scheduleNodes.map((node) =>
    node.widgets.find((item) => item.name === "tag").value), [
    "picture_1", "video_1", "audio_1",
]);
assert.equal(converted.scheduleNodes[1].inputs.find(
    (item) => item.name === "audio").link != null, true);
assert.equal(converted.scheduleNodes[1].inputs.find(
    (item) => item.name === "previous").link != null, true);
assert.equal(converted.wrapper.inputs.find(
    (item) => item.name === "reference_schedule").link != null, true);
assert.equal(converted.wrapper.inputs.find(
    (item) => item.name === "clip_index").link != null, true);
assert.equal(converted.wrapper.inputs.find(
    (item) => item.name === "clip_count").link != null, true);
assert.equal(graph.links[positiveTarget.inputs[0].link].origin_id,
    converted.wrapper.id);
assert.equal(graph.links[latentTarget.inputs[0].link].origin_id,
    converted.wrapper.id);

const dynamicGraph = new FakeGraph();
const dynamicPlan = dynamicGraph.add(new FakeNode(
    PLAN_TYPE, ["generation_fingerprint"], ["plan"]));
const dynamicCurrent = dynamicGraph.add(source(CURRENT_SHOT_TYPE, [
    "clip_index", "clip_count", "source_audio_slice",
]));
const dynamicCore = dynamicGraph.add(makeCore(["ref_audios.ref_audio_0"]));
connectNamed(dynamicCurrent, "source_audio_slice", dynamicCore,
    "ref_audios.ref_audio_0");
const dynamicResult = convertCoreRef2VA(dynamicCore, {createNode: schema});
assert.equal(dynamicResult.connectedFingerprint, false);
assert.equal(dynamicPlan.inputs[0].link, null);

const protectedGraph = new FakeGraph();
const protectedPlan = protectedGraph.add(new FakeNode(
    PLAN_TYPE, ["generation_fingerprint"], ["plan"], [
        ["generation_fingerprint", "model-v2"],
    ]));
const protectedImage = protectedGraph.add(source("LoadImage", ["IMAGE"]));
const protectedCore = protectedGraph.add(makeCore([
    "ref_images.ref_image_0",
]));
connectNamed(protectedImage, "IMAGE", protectedCore,
    "ref_images.ref_image_0");
const protectedResult = convertCoreRef2VA(protectedCore, {createNode: schema});
assert.equal(protectedResult.connectedFingerprint, false);
assert.equal(protectedPlan.inputs[0].link, null);
assert.equal(protectedPlan.widgets[0].value, "model-v2");

const migratedVideo = schema(VIDEO_REF_TYPE);
assert.equal(migrateLegacyVideoScheduleWidgets(migratedVideo, {
    widgets_values: [
        "performance", "4:6", "old declaration", "performance_audio",
        "old audio declaration",
    ],
}), true);
assert.equal(migratedVideo.widgets.find(
    (item) => item.name === "audio_tag").value, "performance_audio");

const extensionSource = fs.readFileSync(
    new URL("../web/h3_reference_autoconnect.js", import.meta.url), "utf8");
assert.match(extensionSource, /Convert to MiniMax H3 Scheduled Ref2VA/);
assert.match(extensionSource, /beforeRegisterNodeDef/);
assert.match(extensionSource, /migrateLegacyVideoScheduleWidgets/);
assert.match(extensionSource, /LiteGraph\?\.createNode/);

console.log("H3 Ref2VA autoconnector: references, loop sockets, outputs, and fingerprint safety pass");
