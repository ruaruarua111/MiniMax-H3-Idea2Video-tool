import assert from "node:assert/strict";
import fs from "node:fs";
import {
    fallbackDisplayIds,
    isRecursiveExecutionId,
    recursiveRootId,
} from "../web/h3_kj_preview_bridge_core.mjs";

assert.equal(isRecursiveExecutionId("1705"), false);
assert.equal(isRecursiveExecutionId("1705.0.0.27"), true);
assert.equal(recursiveRootId("1705.0.0.27"), "1705");
assert.deepEqual(fallbackDisplayIds("1705.0.0.27"), ["27"]);
assert.deepEqual(
    fallbackDisplayIds("1705.0.0.Recurse.0.0.27"),
    ["27"],
);
assert.deepEqual(
    fallbackDisplayIds("1705.0.0.Recurse.0.0.12:27"),
    ["12:27"],
);

const bridgeSource = fs.readFileSync(
    new URL("../web/h3_kj_preview_bridge.js", import.meta.url),
    "utf8",
);
assert.match(bridgeSource, /minimax_h3_context_loop\.kj_preview_loop_bridge/);
assert.doesNotMatch(bridgeSource, /h3_motion_context\.kj_preview_loop_bridge/);

console.log("H3/KJ preview bridge helpers: ok");
