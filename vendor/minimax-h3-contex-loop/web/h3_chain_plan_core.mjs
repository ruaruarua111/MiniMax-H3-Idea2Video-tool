// Pure data helpers for the H3 Chain Plan editor. Keep this module free of
// ComfyUI/browser dependencies so its timing and serialization can be tested.

export const FPS = 24;
export const MAX_SHOTS = 128;
export const MAX_H3_FRAMES = 3592;
export const MAX_SEED = 18446744073709551615n;
export const AUTO_SCENE_COLORS = Object.freeze([
    "#6ea8fe", "#ffb86b", "#63d69f", "#c493ff",
    "#ff7fa6", "#55d6e8", "#e6cb65", "#ff7878",
    "#83c5ff", "#b7db68", "#d991ff", "#72d4c2",
]);

export function automaticSceneColor(index) {
    const ordinal = Number.isFinite(Number(index)) ? Math.trunc(Number(index)) : 0;
    return AUTO_SCENE_COLORS[((ordinal % AUTO_SCENE_COLORS.length)
        + AUTO_SCENE_COLORS.length) % AUTO_SCENE_COLORS.length];
}

function normalizedSeedInteger(value, label = "Seed") {
    try {
        const seed = typeof value === "bigint" ? value : BigInt(value ?? 0);
        if (seed < 0n || seed > MAX_SEED) throw new Error();
        return seed;
    } catch (_error) {
        throw new Error(`${label} must be an unsigned 64-bit integer.`);
    }
}

const SHA256_CONSTANTS = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
    0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
    0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
    0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
    0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
    0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function rotateRight(value, bits) {
    return (value >>> bits) | (value << (32 - bits));
}

function sha256Fallback(bytes) {
    const length = Math.ceil((bytes.length + 9) / 64) * 64;
    const padded = new Uint8Array(length);
    padded.set(bytes);
    padded[bytes.length] = 0x80;
    const bitLength = BigInt(bytes.length) * 8n;
    const view = new DataView(padded.buffer);
    view.setUint32(length - 8, Number((bitLength >> 32n) & 0xffffffffn));
    view.setUint32(length - 4, Number(bitLength & 0xffffffffn));
    const state = new Uint32Array([
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
        0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    ]);
    const words = new Uint32Array(64);
    for (let offset = 0; offset < length; offset += 64) {
        for (let index = 0; index < 16; index += 1) {
            words[index] = view.getUint32(offset + index * 4);
        }
        for (let index = 16; index < 64; index += 1) {
            const left = words[index - 15];
            const right = words[index - 2];
            const sigma0 = rotateRight(left, 7) ^ rotateRight(left, 18) ^ (left >>> 3);
            const sigma1 = rotateRight(right, 17) ^ rotateRight(right, 19) ^ (right >>> 10);
            words[index] = (words[index - 16] + sigma0
                + words[index - 7] + sigma1) >>> 0;
        }
        let [a, b, c, d, e, f, g, h] = state;
        for (let index = 0; index < 64; index += 1) {
            const sum1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
            const choose = (e & f) ^ (~e & g);
            const temporary1 = (h + sum1 + choose
                + SHA256_CONSTANTS[index] + words[index]) >>> 0;
            const sum0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
            const majority = (a & b) ^ (a & c) ^ (b & c);
            const temporary2 = (sum0 + majority) >>> 0;
            h = g;
            g = f;
            f = e;
            e = (d + temporary1) >>> 0;
            d = c;
            c = b;
            b = a;
            a = (temporary1 + temporary2) >>> 0;
        }
        for (const [index, value] of [a, b, c, d, e, f, g, h].entries()) {
            state[index] = (state[index] + value) >>> 0;
        }
    }
    const digest = new Uint8Array(32);
    const digestView = new DataView(digest.buffer);
    state.forEach((value, index) => digestView.setUint32(index * 4, value));
    return digest;
}

export async function derivedSceneSeed(
    baseSeed, index, shotId, cryptoSource = globalThis.crypto,
) {
    const seedBase = normalizedSeedInteger(baseSeed, "Base seed");
    const ordinal = Number(index);
    if (!Number.isInteger(ordinal) || ordinal < 1) {
        throw new Error("Scene index must be a positive integer.");
    }
    const payload = `${seedBase}:${ordinal}:${String(shotId)}`;
    const encoded = new TextEncoder().encode(payload);
    const digest = typeof cryptoSource?.subtle?.digest === "function"
        ? new Uint8Array(await cryptoSource.subtle.digest("SHA-256", encoded))
        : sha256Fallback(encoded);
    const bytes = digest.subarray(0, 8);
    let result = 0n;
    for (const byte of bytes) result = (result << 8n) | BigInt(byte);
    return result.toString();
}

export function randomSceneSeed(randomSource = globalThis.crypto) {
    if (typeof randomSource?.getRandomValues !== "function") {
        throw new Error("Secure browser randomness is unavailable.");
    }
    const words = new Uint32Array(2);
    randomSource.getRandomValues(words);
    return ((BigInt(words[0]) << 32n) | BigInt(words[1])).toString();
}

function clone(value) {
    return JSON.parse(JSON.stringify(value));
}

function protectSeedIntegers(source) {
    // JSON.parse rounds integers above 2^53. The Python node accepts seed
    // strings, so quote numeric `seed` values before parsing and preserve all
    // uint64 digits exactly. This scanner only touches actual JSON object keys,
    // never text that happens to contain `\"seed\": 123` inside a prompt.
    let output = "";
    let index = 0;
    while (index < source.length) {
        if (source[index] !== '"') {
            output += source[index++];
            continue;
        }

        const start = index;
        index += 1;
        while (index < source.length) {
            if (source[index] === "\\") {
                index += 2;
                continue;
            }
            if (source[index] === '"') {
                index += 1;
                break;
            }
            index += 1;
        }
        const token = source.slice(start, index);
        output += token;

        let key;
        try {
            key = JSON.parse(token);
        } catch (_error) {
            continue;
        }
        if (key !== "seed") {
            continue;
        }

        let cursor = index;
        while (/\s/.test(source[cursor] || "")) cursor += 1;
        if (source[cursor] !== ":") continue;
        cursor += 1;
        while (/\s/.test(source[cursor] || "")) cursor += 1;
        const match = source.slice(cursor).match(/^-?\d+(?=\s*[,}])/);
        if (!match) continue;

        output += source.slice(index, cursor);
        output += JSON.stringify(match[0]);
        index = cursor + match[0].length;
    }
    return output;
}

export function promptValueToText(value, label = "prompt") {
    if (Array.isArray(value)) {
        if (!value.every((line) => typeof line === "string")) {
            throw new Error(`${label} line arrays may contain only strings.`);
        }
        return value.join("\n");
    }
    if (value === undefined || value === null) return "";
    return String(value);
}

export function promptTextToLines(value) {
    return String(value ?? "").replace(/\r\n?/g, "\n").split("\n");
}

export function parsePlanJson(source) {
    let raw;
    try {
        raw = JSON.parse(protectSeedIntegers(String(source ?? "")));
    } catch (error) {
        throw new Error(`Invalid plan JSON: ${error.message}`);
    }
    if (Array.isArray(raw)) raw = {shots: raw};
    if (!raw || typeof raw !== "object") {
        throw new Error("The plan must be an object or a bare list of scenes.");
    }
    if (!Array.isArray(raw.shots) || raw.shots.length === 0) {
        throw new Error("The plan needs at least one scene in shots.");
    }
    if (raw.shots.length > MAX_SHOTS) {
        throw new Error(`The plan supports at most ${MAX_SHOTS} scenes.`);
    }

    const plan = clone(raw);
    const hasTopLevelDefaults = Object.hasOwn(plan, "duration_seconds")
        || Object.hasOwn(plan, "steps");
    if (hasTopLevelDefaults) {
        const defaults = plan.defaults
            && typeof plan.defaults === "object"
            && !Array.isArray(plan.defaults)
            ? {...plan.defaults} : {};
        // Accept the common human-authored shorthand and serialize it back in
        // the canonical shape. Explicit defaults.* values win when both forms
        // are present.
        if (!Object.hasOwn(defaults, "duration_seconds")
            && Object.hasOwn(plan, "duration_seconds")) {
            defaults.duration_seconds = plan.duration_seconds;
        }
        if (!Object.hasOwn(defaults, "steps") && Object.hasOwn(plan, "steps")) {
            defaults.steps = plan.steps;
        }
        plan.defaults = defaults;
        delete plan.duration_seconds;
        delete plan.steps;
    }
    plan.shots = plan.shots.map((shot, offset) => {
        if (typeof shot === "string") {
            return {prompt: promptTextToLines(shot)};
        }
        if (!shot || typeof shot !== "object" || Array.isArray(shot)) {
            throw new Error(`Scene ${offset + 1} must be an object or prompt string.`);
        }
        const normalized = {...shot};
        normalized.prompt = promptTextToLines(
            promptValueToText(shot.prompt, `Scene ${offset + 1} prompt`),
        );
        return normalized;
    });

    if (Object.hasOwn(plan, "prompt_prefix")) {
        plan.prompt_prefix = promptTextToLines(
            promptValueToText(plan.prompt_prefix, "prompt_prefix"),
        );
    } else if (Object.hasOwn(plan, "global_prompt")) {
        plan.global_prompt = promptTextToLines(
            promptValueToText(plan.global_prompt, "global_prompt"),
        );
    }
    return plan;
}

export function planToJson(plan) {
    return JSON.stringify(plan, null, 2);
}

export function sharedPrompt(plan) {
    const key = Object.hasOwn(plan, "prompt_prefix")
        ? "prompt_prefix"
        : Object.hasOwn(plan, "global_prompt") ? "global_prompt" : "prompt_prefix";
    return {key, text: promptValueToText(plan[key] ?? "", key)};
}

export function setSharedPrompt(plan, text) {
    const current = sharedPrompt(plan);
    plan[current.key] = promptTextToLines(text);
}

export function safeShotId(value, fallback) {
    let text = String(value ?? "").trim().replace(/[^A-Za-z0-9._-]+/g, "_");
    text = text.replace(/^[._-]+|[._-]+$/g, "");
    return (text || fallback).slice(0, 96);
}

export function uniqueShotId(shots, requested = "scene") {
    const used = new Set(shots.map((shot, offset) => safeShotId(
        shot?.id,
        `clip_${String(offset + 1).padStart(4, "0")}`,
    )));
    const base = safeShotId(requested, "scene");
    if (!used.has(base)) return base;
    for (let suffix = 2; suffix <= MAX_SHOTS + 1; suffix += 1) {
        const candidate = `${base}_${suffix}`.slice(0, 96);
        if (!used.has(candidate)) return candidate;
    }
    return `${base}_${Date.now()}`.slice(0, 96);
}

export function makeShot(shots = []) {
    const ordinal = shots.length + 1;
    return {
        id: uniqueShotId(shots, `scene_${String(ordinal).padStart(2, "0")}`),
        prompt: ["Describe this scene."],
    };
}

export function duplicateShot(shots, index) {
    const duplicated = clone(shots[index]);
    duplicated.id = uniqueShotId(shots, `${safeShotId(
        duplicated.id,
        `scene_${String(index + 1).padStart(2, "0")}`,
    )}_copy`);
    shots.splice(index + 1, 0, duplicated);
    return duplicated;
}

export function moveShot(shots, from, to) {
    if (from === to || from < 0 || from >= shots.length || to < 0 || to >= shots.length) {
        return;
    }
    const [shot] = shots.splice(from, 1);
    shots.splice(to, 0, shot);
}

export function h3FrameLength(seconds) {
    const numeric = Number(seconds);
    if (!Number.isFinite(numeric) || numeric <= 0) {
        throw new Error("Duration must be a finite positive number.");
    }
    const requested = Math.max(5, Math.ceil(numeric * FPS - 1e-9));
    const length = requested + ((5 - (requested % 17)) % 17);
    if (length > MAX_H3_FRAMES) {
        throw new Error(
            `Duration rounds to ${length} frames; H3's largest valid length is ${MAX_H3_FRAMES}.`,
        );
    }
    return length;
}

export function validateH3Length(value) {
    const length = Number(value);
    if (!Number.isInteger(length) || length < 5 || length > MAX_H3_FRAMES || length % 17 !== 5) {
        throw new Error(
            `Exact length must be 5..${MAX_H3_FRAMES} frames with length % 17 == 5.`,
        );
    }
    return length;
}

function sceneRawFrames(shot, defaultDuration) {
    if (shot.length !== undefined && shot.length !== null && shot.length !== "") {
        return validateH3Length(shot.length);
    }
    if (shot.frames !== undefined && shot.frames !== null && shot.frames !== "") {
        return validateH3Length(shot.frames);
    }
    const duration = shot.duration_seconds ?? defaultDuration;
    return h3FrameLength(duration);
}

function validateSeed(seed) {
    if (seed === undefined || seed === null || seed === "") return;
    let numeric;
    try {
        numeric = BigInt(String(seed));
    } catch (_error) {
        throw new Error("Seed must be an unsigned 64-bit integer.");
    }
    if (numeric < 0n || numeric > MAX_SEED) {
        throw new Error("Seed is outside the unsigned 64-bit range.");
    }
}

export function calculatePlanTiming(plan, settings = {}) {
    const errors = [];
    const rows = [];
    const contextLength = Number(settings.contextLength ?? 22);
    const anchorMode = settings.anchorMode ?? "head";
    const nodeDefaultDuration = Number(settings.defaultDurationSeconds ?? 15);
    const planDefaultDuration = Number(plan?.defaults?.duration_seconds ?? nodeDefaultDuration);
    const defaultSteps = Number(plan?.defaults?.steps ?? settings.defaultSteps ?? 20);
    const hasSharedPrompt = sharedPrompt(plan ?? {}).text.trim().length > 0;

    if (![1, 5, 22, 39].includes(contextLength)) {
        errors.push("Context length must be 1, 5, 22, or 39.");
    }
    if (!Number.isFinite(planDefaultDuration) || planDefaultDuration <= 0) {
        errors.push("Default duration must be a finite positive number.");
    }
    if (!Number.isInteger(defaultSteps) || defaultSteps < 1 || defaultSteps > 10000) {
        errors.push("Default steps must be between 1 and 10000.");
    }

    const ids = new Set();
    let stitchedFrames = 0;
    for (let offset = 0; offset < (plan?.shots?.length ?? 0); offset += 1) {
        const shot = plan.shots[offset];
        const index = offset + 1;
        const fallback = `clip_${String(index).padStart(4, "0")}`;
        const id = safeShotId(shot.id, fallback);
        const rowErrors = [];
        if (ids.has(id)) rowErrors.push(`Duplicate normalized scene id “${id}”.`);
        ids.add(id);
        if (!promptValueToText(shot.prompt, `Scene ${index} prompt`).trim()
            && !hasSharedPrompt) {
            rowErrors.push("Scene and shared prompts are both empty.");
        }

        const steps = Number(shot.steps ?? defaultSteps);
        if (!Number.isInteger(steps) || steps < 1 || steps > 10000) {
            rowErrors.push("Steps must be between 1 and 10000.");
        }
        try {
            validateSeed(shot.seed);
        } catch (error) {
            rowErrors.push(error.message);
        }

        let rawFrames = 0;
        try {
            rawFrames = sceneRawFrames(shot, planDefaultDuration);
        } catch (error) {
            rowErrors.push(error.message);
        }

        let deliveredFrames = rawFrames;
        let generationStartFrame = stitchedFrames;
        if (index > 1 && anchorMode === "head") {
            if (rawFrames <= contextLength) {
                rowErrors.push(
                    `${rawFrames} raw frames are not longer than the ${contextLength}-frame overlap.`,
                );
            }
            deliveredFrames = Math.max(0, rawFrames - contextLength);
            generationStartFrame = stitchedFrames - contextLength;
        }

        rows.push({
            index,
            id,
            rawFrames,
            rawSeconds: rawFrames / FPS,
            deliveredFrames,
            deliveredSeconds: deliveredFrames / FPS,
            generationStartFrame,
            errors: rowErrors,
        });
        stitchedFrames += deliveredFrames;
    }

    for (let offset = 0; offset < rows.length - 1; offset += 1) {
        if (rows[offset].deliveredFrames < contextLength) {
            rows[offset].errors.push(
                `Delivers fewer than ${contextLength} frames needed by the next scene.`,
            );
        }
    }
    for (const row of rows) {
        for (const error of row.errors) errors.push(`Scene ${row.index}: ${error}`);
    }

    return {
        shots: rows,
        totalFrames: stitchedFrames,
        totalSeconds: stitchedFrames / FPS,
        errors,
    };
}

export function formatClock(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "—";
    const wholeMinutes = Math.floor(seconds / 60);
    const remainder = seconds - wholeMinutes * 60;
    return wholeMinutes
        ? `${wholeMinutes}:${remainder.toFixed(3).padStart(6, "0")}`
        : `${remainder.toFixed(3)}s`;
}
