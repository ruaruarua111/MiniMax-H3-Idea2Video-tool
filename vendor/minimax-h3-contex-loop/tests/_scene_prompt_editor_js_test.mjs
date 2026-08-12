import assert from "node:assert/strict";
import fs from "node:fs";
import {
    parsePlanJson,
    planToJson,
    promptTextToLines,
    promptValueToText,
} from "../web/h3_chain_plan_core.mjs";

const plan = parsePlanJson(JSON.stringify({
    prompt_prefix: "Shared identity.",
    shots: [
        {id: "one", prompt: "Old one."},
        {id: "two", prompt: ["Old two.", "", "CAMERA: Wide."]},
    ],
}));
plan.shots[1].prompt = promptTextToLines(
    "Continue the action.\n\n<Picture 1> remains the identity reference.",
);
const saved = parsePlanJson(planToJson(plan));
assert.equal(promptValueToText(saved.shots[0].prompt), "Old one.");
assert.equal(
    promptValueToText(saved.shots[1].prompt),
    "Continue the action.\n\n<Picture 1> remains the identity reference.",
);

const source = fs.readFileSync(
    new URL("../web/h3_chain_scene_prompt_editor.js", import.meta.url),
    "utf8",
);
assert.match(source, /MiniMaxH3ChainScenePromptEditor/);
assert.match(source, /item\.name === "plan_json"/);
assert.match(source, /shot\.prompt = promptTextToLines\(textarea\.value\)/);
assert.match(source, /state\.planWidget\.value = value/);
assert.match(source, /_h3ChainEditorRefresh/);
assert.match(source, /Alt\+Left/);
assert.match(source, /Alt\+Right/);
assert.match(source, /@ Reference/);
assert.match(source, /availableReferenceRecords/);
assert.match(source, /does not invent unavailable labels/);
assert.doesNotMatch(source, /Generic native labels are shown instead/);
assert.match(source, /Hover to preview/);
assert.match(source, /Audio never autoplays/);
assert.match(source, /h3sp-ref-preview-media/);
assert.match(source, /record\.kind === "picture" \? "image"/);
assert.match(source, /showReferencePreview\(records\[0\], preview\)/);
assert.match(source, /Core Ref2VA references/);
assert.match(source, /# Dialogue/);
assert.match(source, /FONT_SIZE_PROPERTY/);
assert.match(source, /const PROMPT_ASSISTANT_ENABLED = false;/);
assert.match(source, /if \(PROMPT_ASSISTANT_ENABLED\) \{\s*assistant\.client = new PromptAssistantClient/);
assert.match(source, /getMinHeight: \(\) => PROMPT_ASSISTANT_ENABLED \? 760 : 420/);
assert.match(source, /const minimumHeight = PROMPT_ASSISTANT_ENABLED \? 900 : 620/);
assert.match(source, /currentWidth === 760/);
assert.match(source, /currentHeight === 900/);
assert.match(source, /PromptAssistantClient/);
assert.match(source, /buildPromptAssistantContext/);
assert.match(source, /prompt_assist_result/);
assert.match(source, /Staged .* proposal/);
assert.match(source, /Apply to scene/);
assert.match(source, /Apply anyway/);
assert.match(source, /messagesByProvider/);
assert.match(source, /promptAssistantIdentityKey/);
assert.match(source, /activeWorkflow/);
assert.match(source, /persistPendingRequest/);
assert.match(source, /restorePendingRequest/);
assert.match(source, /Accept only a request this editor incarnation/);
assert.match(source, /assistant\.preparingRequest !== preparation/);
assert.match(source, /Snapshot the selected scene before the asynchronous bridge handshake/);
assert.match(source, /prompt_assist_cancel_ack/);
assert.match(source, /empty assistant draft cannot replace/i);
assert.match(source, /Undo last apply/);
assert.match(source, /assistant\.client\?\.close\(\)/);
assert.match(source, /window\.setInterval\(\(\) => loadPlan\(false\), 500\)/);

console.log("H3 Scene Prompt companion: Plan synchronization and controls pass");
