import assert from "node:assert/strict";
import {
    availableReferenceRecords,
    collectScheduleNodes,
    coreReferenceRecords,
    findScheduledRef2VA,
    imageToVideoReferenceRecords,
    referencePreviewRecords,
    referenceIsActive,
    scheduledReferenceRecords,
} from "../web/h3_reference_preview_core.mjs";

function makeNode(id, type, widgets = {}) {
    return {
        id,
        type,
        title: type,
        widgets: Object.entries(widgets).map(([name, value]) => ({name, value})),
        inputs: [],
        outputs: [{name: "output", links: []}],
    };
}

const graph = {
    _nodes: [],
    links: {},
    getNodeById(id) { return this._nodes.find((node) => node.id === id); },
};
let nextLink = 1;
function add(node) {
    node.graph = graph;
    graph._nodes.push(node);
    return node;
}
function connect(source, target, targetName) {
    const id = nextLink++;
    source.outputs[0].links.push(id);
    target.inputs.push({name: targetName, link: id});
    graph.links[id] = {
        origin_id: source.id,
        origin_slot: 0,
        target_id: target.id,
        target_slot: target.inputs.length - 1,
    };
}

const editor = add(makeNode(1, "MiniMaxH3ChainScenePromptEditor"));
const relay = add(makeNode(2, "MiniMaxH3ChainCurrent"));
const wrapper = add(makeNode(3, "MiniMaxH3ScheduledReferenceToVideo"));
const firstImage = add(makeNode(4, "LoadImage", {image: "first.png"}));
const secondImage = add(makeNode(5, "LoadImage", {image: "second.png"}));
const audioFile = add(makeNode(6, "LoadAudio", {audio: "score.wav"}));
const first = add(makeNode(7, "MiniMaxH3ScheduledPictureReference", {
    tag: "picture_1", scenes: "1",
}));
const second = add(makeNode(8, "MiniMaxH3ScheduledPictureReference", {
    tag: "picture_2", scenes: "",
}));
const audio = add(makeNode(9, "MiniMaxH3ScheduledAudioReference", {
    tag: "score", scenes: "all",
}));

connect(editor, relay, "state");
connect(relay, wrapper, "prompt");
connect(firstImage, first, "image");
connect(first, second, "previous");
connect(secondImage, second, "image");
connect(second, audio, "previous");
connect(audioFile, audio, "audio");
connect(audio, wrapper, "reference_schedule");

assert.equal(findScheduledRef2VA(editor), wrapper);
assert.deepEqual(collectScheduleNodes(wrapper), [first, second, audio]);
assert.equal(referenceIsActive("1,3:5", 4), true);
assert.equal(referenceIsActive("1,3:5", 2), false);

const sceneOne = scheduledReferenceRecords(editor, 1).records;
assert.deepEqual(sceneOne.map(({tag, label, active}) => ({tag, label, active})), [
    {tag: "picture_1", label: "<Picture 1>", active: true},
    {tag: "picture_2", label: "<Picture 2>", active: true},
    {tag: "score", label: "<Audio 1>", active: true},
]);
const sceneTwo = scheduledReferenceRecords(editor, 2).records;
assert.deepEqual(sceneTwo.map(({tag, label, active}) => ({tag, label, active})), [
    {tag: "picture_1", label: null, active: false},
    {tag: "picture_2", label: "<Picture 1>", active: true},
    {tag: "score", label: "<Audio 1>", active: true},
]);
assert.deepEqual(
    availableReferenceRecords(editor, 2).records.map(({tag}) => tag),
    ["picture_2", "score"],
);
assert.deepEqual(
    availableReferenceRecords(editor, 2, {includeInactive: true})
        .records.map(({tag}) => tag),
    ["picture_1", "picture_2", "score"],
);

const coreEditor = add(makeNode(10, "MiniMaxH3ChainScenePromptEditor"));
const coreRelay = add(makeNode(11, "MiniMaxH3ChainCurrent"));
const core = add(makeNode(12, "MiniMaxH3ReferenceToVideo"));
const coreImage = add(makeNode(13, "LoadImage", {image: "core.png"}));
const coreAudio = add(makeNode(14, "LoadAudio", {audio: "core.wav"}));
connect(coreEditor, coreRelay, "state");
connect(coreRelay, core, "prompt");
connect(coreImage, core, "ref_images.ref_image_0");
connect(coreAudio, core, "ref_audios.ref_audio_0");
const native = coreReferenceRecords(coreEditor);
assert.equal(native.mode, "native");
assert.deepEqual(native.records.map(({kind, token, label}) => ({kind, token, label})), [
    {kind: "picture", token: "<Picture 1>", label: "<Picture 1>"},
    {kind: "audio", token: "<Audio 1>", label: "<Audio 1>"},
]);
assert.equal(referencePreviewRecords(coreEditor, 1).mode, "native");
assert.deepEqual(
    availableReferenceRecords(coreEditor, 1).records.map(({token}) => token),
    ["<Picture 1>", "<Audio 1>"],
);

const flEditor = add(makeNode(15, "MiniMaxH3ChainScenePromptEditor"));
const flRelay = add(makeNode(16, "MiniMaxH3ChainCurrent"));
const fl2v = add(makeNode(17, "MiniMaxH3ImageToVideo"));
const firstFrame = add(makeNode(18, "LoadImage", {image: "first.png"}));
const lastFrame = add(makeNode(19, "LoadImage", {image: "last.png"}));
connect(flEditor, flRelay, "state");
connect(flRelay, fl2v, "prompt");
connect(firstFrame, fl2v, "first_frame");
connect(lastFrame, fl2v, "last_frame");
const keyframes = imageToVideoReferenceRecords(flEditor);
assert.equal(keyframes.mode, "native_keyframes");
assert.deepEqual(
    keyframes.records.map(({token, role}) => ({token, role})),
    [
        {token: "<Picture 1>", role: "first frame"},
        {token: "<Picture 2>", role: "last frame"},
    ],
);
assert.equal(referencePreviewRecords(flEditor, 1).mode, "native_keyframes");

const i2vEditor = add(makeNode(24, "MiniMaxH3ChainScenePromptEditor"));
const i2vRelay = add(makeNode(25, "MiniMaxH3ChainCurrent"));
const i2v = add(makeNode(26, "MiniMaxH3ImageToVideo"));
const openingFrame = add(makeNode(27, "LoadImage", {image: "opening.png"}));
const firstSceneGate = add(makeNode(28, "MiniMaxH3ChainFirstSceneImage"));
connect(i2vEditor, i2vRelay, "state");
connect(i2vRelay, i2v, "prompt");
connect(openingFrame, firstSceneGate, "image");
connect(firstSceneGate, i2v, "first_frame");
assert.deepEqual(
    imageToVideoReferenceRecords(i2vEditor, 1).records
        .map(({token, selector, active, source}) => ({
            token, selector, active, source: source.type,
        })),
    [{token: "<Picture 1>", selector: "1", active: true,
        source: "MiniMaxH3ChainFirstSceneImage"}],
);
assert.deepEqual(
    referencePreviewRecords(i2vEditor, 2).records
        .map(({token, selector, active}) => ({token, selector, active})),
    [{token: "<Picture 1>", selector: "1", active: false}],
);
assert.deepEqual(availableReferenceRecords(i2vEditor, 2).records, []);

const lEditor = add(makeNode(20, "MiniMaxH3ChainScenePromptEditor"));
const lRelay = add(makeNode(21, "MiniMaxH3ChainCurrent"));
const l2v = add(makeNode(22, "MiniMaxH3ImageToVideo"));
const onlyLastFrame = add(makeNode(23, "LoadImage", {image: "last-only.png"}));
connect(lEditor, lRelay, "state");
connect(lRelay, l2v, "prompt");
connect(onlyLastFrame, l2v, "last_frame");
assert.deepEqual(
    imageToVideoReferenceRecords(lEditor).records.map(({token, role}) => ({token, role})),
    [{token: "<Picture 1>", role: "last frame"}],
);

console.log("H3 reference preview: scheduled Ref2VA, core Ref2VA, and core I2V/FL2V discovery pass");
