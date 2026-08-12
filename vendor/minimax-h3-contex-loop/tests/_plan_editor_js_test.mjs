#!/usr/bin/env node

import assert from "node:assert/strict";
import fs from "node:fs";
import {
    AUTO_SCENE_COLORS,
    automaticSceneColor,
    calculatePlanTiming,
    derivedSceneSeed,
    duplicateShot,
    h3FrameLength,
    moveShot,
    parsePlanJson,
    planToJson,
    promptValueToText,
    randomSceneSeed,
    setSharedPrompt,
    sharedPrompt,
    validateH3Length,
} from "../web/h3_chain_plan_core.mjs";

assert.equal(AUTO_SCENE_COLORS.length, 12);
assert.equal(new Set(AUTO_SCENE_COLORS).size, AUTO_SCENE_COLORS.length);
assert.equal(automaticSceneColor(0), AUTO_SCENE_COLORS[0]);
assert.equal(automaticSceneColor(12), AUTO_SCENE_COLORS[0]);
assert.equal(automaticSceneColor(-1), AUTO_SCENE_COLORS.at(-1));
assert.equal(await derivedSceneSeed(0, 1, "intro"), "2670204060324819354");
assert.equal(await derivedSceneSeed(42, 2, "scene_02"), "7780599706863635211");
assert.equal(
    await derivedSceneSeed(42, 2, "scene_02", {}),
    "7780599706863635211",
);
assert.equal(randomSceneSeed({
    getRandomValues(words) {
        words[0] = 0x12345678;
        words[1] = 0x9abcdef0;
        return words;
    },
}), "1311768467463790320");

const plan = parsePlanJson(JSON.stringify({
    prompt_prefix: ["Identity.", "", "Wardrobe."],
    defaults: {duration_seconds: 15, steps: 20},
    shots: [
        {id: "one", prompt: "Opening.\nKeep moving.", seed: 18446744073709551615n.toString()},
        {id: "two", prompt: ["Continue.", "", "End turning."], length: 260},
    ],
}));

assert.equal(sharedPrompt(plan).text, "Identity.\n\nWardrobe.");
assert.equal(promptValueToText(plan.shots[0].prompt), "Opening.\nKeep moving.");
setSharedPrompt(plan, "New identity.\n\nNew wardrobe.");
assert.deepEqual(plan.prompt_prefix, ["New identity.", "", "New wardrobe."]);
assert.equal(JSON.parse(planToJson(plan)).shots[0].seed, "18446744073709551615");

const numericSeed = parsePlanJson(
    '{"shots":[{"id":"seed","prompt":"x","seed":18446744073709551615}]}',
);
assert.equal(numericSeed.shots[0].seed, "18446744073709551615");
const promptContainingSeedText = parsePlanJson(
    '{"shots":[{"prompt":"Literal \\\"seed\\\": 18446744073709551615 text"}]}',
);
assert.equal(
    promptValueToText(promptContainingSeedText.shots[0].prompt),
    'Literal "seed": 18446744073709551615 text',
);

const shorthandDefaults = parsePlanJson(JSON.stringify({
    duration_seconds: 8,
    steps: 10,
    shots: [{
        id: "imported",
        prompt: "Imported prompt.",
        duration_seconds: 6,
        steps: 12,
    }],
}));
assert.deepEqual(shorthandDefaults.defaults, {duration_seconds: 8, steps: 10});
assert.equal(Object.hasOwn(shorthandDefaults, "duration_seconds"), false);
assert.equal(Object.hasOwn(shorthandDefaults, "steps"), false);
assert.equal(shorthandDefaults.shots[0].duration_seconds, 6);
assert.equal(shorthandDefaults.shots[0].steps, 12);

assert.equal(h3FrameLength(5), 124);
assert.equal(h3FrameLength(10), 243);
assert.equal(h3FrameLength(15), 362);
assert.equal(validateH3Length(260), 260);
assert.throws(() => validateH3Length(240), /length % 17/);

const timing = calculatePlanTiming(plan, {
    contextLength: 22,
    anchorMode: "head",
    defaultDurationSeconds: 5,
    defaultSteps: 10,
});
assert.deepEqual(timing.shots.map((shot) => shot.rawFrames), [362, 260]);
assert.deepEqual(timing.shots.map((shot) => shot.deliveredFrames), [362, 238]);
assert.equal(timing.shots[1].generationStartFrame, 340);
assert.equal(timing.totalFrames, 600);
assert.deepEqual(timing.errors, []);

const sharedOnlyPlan = parsePlanJson(JSON.stringify({
    prompt_prefix: "Shared identity and direction.",
    shots: [{id: "shared_only", prompt: ""}],
}));
const sharedOnlyTiming = calculatePlanTiming(sharedOnlyPlan, {
    contextLength: 22,
    anchorMode: "head",
    defaultDurationSeconds: 5,
    defaultSteps: 10,
});
assert.deepEqual(sharedOnlyTiming.errors, []);

const fullyEmptyPlan = parsePlanJson(JSON.stringify({
    shots: [{id: "empty", prompt: ""}],
}));
const fullyEmptyTiming = calculatePlanTiming(fullyEmptyPlan, {
    contextLength: 22,
    anchorMode: "head",
    defaultDurationSeconds: 5,
    defaultSteps: 10,
});
assert.match(fullyEmptyTiming.errors.join("\n"), /scene and shared prompts are both empty/i);

const longPlan = parsePlanJson(JSON.stringify({
    defaults: {duration_seconds: 15, steps: 5},
    shots: Array.from({length: 14}, (_, index) => ({
        id: `clip_${String(index + 1).padStart(2, "0")}`,
        prompt: `Scene ${index + 1}`,
        ...(index === 13 ? {duration_seconds: 5} : {}),
    })),
}));
const longTiming = calculatePlanTiming(longPlan, {
    contextLength: 22,
    anchorMode: "head",
    defaultDurationSeconds: 15,
    defaultSteps: 20,
});
assert.equal(longTiming.totalFrames, 4544);
assert.equal(longTiming.totalSeconds, 189 + 1 / 3);
assert.deepEqual(longTiming.errors, []);

duplicateShot(plan.shots, 0);
assert.equal(plan.shots.length, 3);
assert.equal(plan.shots[1].id, "one_copy");
moveShot(plan.shots, 1, 2);
assert.equal(plan.shots[2].id, "one_copy");

const readable = JSON.parse(planToJson(plan));
assert.deepEqual(readable.prompt_prefix, ["New identity.", "", "New wardrobe."]);
assert.deepEqual(readable.shots[0].prompt, ["Opening.", "Keep moving."]);

const editorSource = fs.readFileSync(
    new URL("../web/h3_chain_plan_editor.js", import.meta.url),
    "utf8",
);
assert.match(editorSource, /collapseWidget\(planWidget\)/);
assert.match(editorSource, /display[^\n]+none[^\n]+important/);
assert.match(editorSource, /onGraphConfigured/);
assert.match(editorSource, /scheduleResponsiveSize\(\)/);
assert.doesNotMatch(editorSource, /height: \$\{EDITOR_HEIGHT\}px/);
assert.match(editorSource, /node\.size\?\.\[1\][^\n]+0,/);
assert.doesNotMatch(editorSource, /const computed = node\.computeSize/);
assert.match(editorSource, /h3_chain_plan_layout/);
assert.match(editorSource, /new ResizeObserver/);
assert.match(editorSource, /"pointerdown", "pointerup", "mousedown", "mouseup", "click"/);
assert.match(editorSource, /availableReferenceRecords/);
assert.doesNotMatch(editorSource, /\[\["Picture", 9\], \["Video", 3\], \["Audio", 6\]\]/);
assert.match(editorSource, /Derived seed:/);
assert.match(editorSource, /New random/);
assert.match(editorSource, /Use derived/);
assert.match(editorSource, /h3_chain_scene_colors/);
assert.match(editorSource, /type = "color"/);
assert.match(editorSource, /minimax_h3_context_loop\.chain_plan_editor/);
assert.match(editorSource, /function folderOpenIcon\(\)/);
assert.match(editorSource, /createElementNS\(namespace, "svg"\)/);
assert.match(editorSource, /h3c-folder-icon/);
assert.match(editorSource, /minimax_h3_context_loop\/open-run-folder/);
assert.match(editorSource, /navigator\.clipboard\.writeText\(payload\.path\)/);
assert.doesNotMatch(editorSource, /h3_motion_context\.chain_plan_editor/);

console.log("H3 Chain Plan editor core: parsing, uint64 seeds, timing and edits pass");
