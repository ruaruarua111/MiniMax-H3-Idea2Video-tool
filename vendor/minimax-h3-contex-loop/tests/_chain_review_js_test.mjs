import assert from "node:assert/strict";
import fs from "node:fs";
import {
    applyReviewEdit,
    checkpointResumeOptions,
    reviewCountdown,
    reviewLocalDeadline,
    reviewSeed,
} from "../web/h3_chain_review_core.mjs";

assert.equal(reviewSeed("18446744073709551615"), "18446744073709551615");
assert.throws(() => reviewSeed("18446744073709551616"), /uint64/);

const plan = {
    prompt_prefix: ["Keep identity."],
    shots: [
        {id: "one", prompt: ["Old one."], seed: "1"},
        {id: "two", prompt: ["Old two."], seed: "2"},
    ],
};
applyReviewEdit(plan, 2, "New two.\n\nCAMERA: Close-up.", "9007199254740993");
assert.deepEqual(plan.shots[0].prompt, ["Old one."]);
assert.deepEqual(plan.shots[1].prompt, ["New two.", "", "CAMERA: Close-up."]);
assert.equal(plan.shots[1].seed, "9007199254740993");
applyReviewEdit(plan, 1, "", "3");
assert.deepEqual(plan.shots[0].prompt, [""]);
assert.equal(plan.shots[0].seed, "3");
assert.throws(
    () => applyReviewEdit({shots: [{prompt: [""]}]}, 1, "", "4"),
    /scene prompt or shared prompt/i,
);

assert.deepEqual(reviewCountdown(130, 100_000), {seconds: 30, text: "0:30"});
assert.deepEqual(reviewCountdown(100, 100_001), {seconds: 0, text: "0:00"});
assert.equal(reviewCountdown(null, 0), null);
assert.equal(reviewLocalDeadline(null, 100, 100_000), null);
assert.equal(reviewLocalDeadline(undefined, 100, 100_000), null);
assert.equal(reviewLocalDeadline("", 100, 100_000), null);
assert.equal(reviewLocalDeadline(130, 100, 100_000), 130);

assert.deepEqual(checkpointResumeOptions([
    {scene: 2, resume_scene: 3, scene_id: "second", ready: true,
        video: {filename: "second.mp4"}},
    {scene: 1, resume_scene: 2, scene_id: "first", ready: true,
        partial_video: {filename: "partial.mp4"}},
    {scene: 3, resume_scene: 4, scene_id: "final", ready: true},
    {scene: 1, resume_scene: 2, scene_id: "broken", ready: false},
], 3), [
    {savedScene: 1, resumeScene: 2, sceneId: "first", video: null,
        partialVideo: {filename: "partial.mp4"}},
    {savedScene: 2, resumeScene: 3, sceneId: "second",
        video: {filename: "second.mp4"}, partialVideo: null},
]);

const reviewSource = fs.readFileSync(
    new URL("../web/h3_chain_review.js", import.meta.url),
    "utf8",
);
assert.match(reviewSource, /minimax_h3_context_loop\/review/);
assert.match(reviewSource, /minimax_h3_context_loop_review_resolved/);
assert.match(reviewSource, /item\.name === "scene_range"/);
assert.match(reviewSource, /rangeWidget\.value = ""/);
assert.match(reviewSource, /_h3QueuedReview/);
assert.match(reviewSource, /setInterval[\s\S]*fetchPending/);
assert.match(reviewSource, /addEventListener\("status", fetchPending\)/);
assert.match(reviewSource, /async nodeCreated\(node\)/);
assert.match(reviewSource, /gates\.length === 1/);
assert.match(reviewSource, /"pointerdown", "pointerup", "mousedown", "mouseup", "click"/);
assert.match(reviewSource, /preview_revision/);
assert.match(reviewSource, /sameToken/);
assert.match(reviewSource, /h3r-video-panel/);
assert.match(reviewSource, /h3r-video-grip/);
assert.match(reviewSource, /h3_chain_review_video_height/);
assert.match(reviewSource, /h3_chain_review_prompt_height/);
assert.match(reviewSource, /promptResizeObserver = new ResizeObserver/);
assert.match(reviewSource, /_h3ReviewApplyLayout/);
assert.match(reviewSource, /nodeType\.prototype\.onConfigure/);
assert.match(reviewSource, /setPointerCapture/);
assert.match(reviewSource, /visualHeight \/ layoutHeight/);
assert.match(reviewSource, /videoPanel\.offsetHeight, true/);
assert.doesNotMatch(reviewSource, /\/h3_motion_context\/review/);

console.log("H3 Chain Review editor helpers: ok");
