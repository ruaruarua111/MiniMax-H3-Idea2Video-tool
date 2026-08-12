import {MAX_SEED, promptTextToLines, sharedPrompt} from "./h3_chain_plan_core.mjs";

export function reviewSeed(value) {
    let seed;
    try {
        seed = BigInt(String(value));
    } catch (_error) {
        throw new Error("Seed must be an integer.");
    }
    if (seed < 0n || seed > MAX_SEED) {
        throw new Error("Seed is outside the uint64 range.");
    }
    return seed.toString();
}

export function applyReviewEdit(plan, oneBasedIndex, scenePrompt, seed) {
    const index = Number(oneBasedIndex) - 1;
    if (!Array.isArray(plan?.shots) || index < 0 || index >= plan.shots.length) {
        throw new Error("The reviewed scene does not exist in the plan.");
    }
    const prompt = String(scenePrompt ?? "").replace(/\r\n?/g, "\n").trim();
    if (!prompt && !sharedPrompt(plan).text.trim()) {
        throw new Error("Retry requires a scene prompt or shared prompt.");
    }
    const normalizedSeed = reviewSeed(seed);
    plan.shots[index].prompt = promptTextToLines(prompt);
    plan.shots[index].seed = normalizedSeed;
    return plan;
}

export function reviewCountdown(deadlineSeconds, nowMilliseconds = Date.now()) {
    if (deadlineSeconds === null || deadlineSeconds === undefined || deadlineSeconds === "") {
        return null;
    }
    const deadline = Number(deadlineSeconds);
    if (!Number.isFinite(deadline)) return null;
    const seconds = Math.max(0, Math.ceil(deadline - Number(nowMilliseconds) / 1000));
    const minutes = Math.floor(seconds / 60);
    const remainder = String(seconds % 60).padStart(2, "0");
    return {seconds, text: `${minutes}:${remainder}`};
}

export function reviewLocalDeadline(
    deadlineSeconds,
    serverNowSeconds,
    clientNowMilliseconds = Date.now(),
) {
    if (deadlineSeconds === null || deadlineSeconds === undefined || deadlineSeconds === "") {
        return null;
    }
    const deadline = Number(deadlineSeconds);
    const serverNow = Number(serverNowSeconds);
    if (!Number.isFinite(deadline) || !Number.isFinite(serverNow)) return null;
    return Number(clientNowMilliseconds) / 1000 + Math.max(0, deadline - serverNow);
}

export function checkpointResumeOptions(checkpoints, clipCount) {
    const total = Number(clipCount);
    if (!Number.isInteger(total) || total < 1) return [];
    const byResumeScene = new Map();
    for (const item of checkpoints ?? []) {
        const savedScene = Number(item?.scene);
        const resumeScene = Number(item?.resume_scene ?? savedScene + 1);
        if (!item?.ready || !Number.isInteger(savedScene) || savedScene < 1
            || !Number.isInteger(resumeScene) || resumeScene < 2
            || resumeScene > total) continue;
        byResumeScene.set(resumeScene, {
            savedScene,
            resumeScene,
            sceneId: String(item.scene_id ?? `clip_${String(savedScene).padStart(4, "0")}`),
            video: item.video ?? null,
            partialVideo: item.partial_video ?? null,
        });
    }
    return [...byResumeScene.values()].sort((left, right) =>
        left.resumeScene - right.resumeScene);
}
