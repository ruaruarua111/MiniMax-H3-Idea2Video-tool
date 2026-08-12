export const H3_LOOP_END_TYPE = "MiniMaxH3ChainLoopEnd";
export const KJ_PREVIEW_TYPE = "ModelPreviewOverrideKJ";

export function recursiveRootId(executionId) {
    const value = String(executionId ?? "");
    const dot = value.indexOf(".");
    return dot > 0 ? value.slice(0, dot) : null;
}

export function isRecursiveExecutionId(executionId) {
    return recursiveRootId(executionId) !== null;
}

export function fallbackDisplayIds(executionId) {
    const value = String(executionId ?? "");
    if (!isRecursiveExecutionId(value)) return [];

    // GraphBuilder prefixes every recursive clone with dot-separated execution
    // components. Canvas/subgraph display ids use ':' and therefore remain
    // intact as the final component.
    const leaf = value.slice(value.lastIndexOf(".") + 1);
    return leaf && leaf !== value ? [leaf] : [];
}
