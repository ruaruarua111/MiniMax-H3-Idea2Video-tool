import assert from "node:assert/strict";
import {
    buildPromptAssistantContext,
    draftConflict,
    makePromptAssistRequest,
    promptSceneKey,
    promptSourceRevision,
} from "../web/h3_prompt_assistant_core.mjs";
import {
    discoverPromptAssistBridge,
    PromptAssistantClient,
} from "../web/h3_prompt_assistant_client.mjs";

const plan = {
    prompt_prefix: ["Keep <Picture 1> identity and the red coat."],
    defaults: {duration_seconds: 10},
    shots: [
        {id: "entry", prompt: ["End while she reaches the door."]},
        {id: "hall", prompt: ["She crosses the hall.", "<d>Wait for me.</d>"]},
        {id: "street", prompt: ["Continue through the already-opening door."]},
    ],
};
const source = "She crosses the hall.\n<d>Wait for me.</d>";
const context = buildPromptAssistantContext(plan, 1, source, {
    includeShared: true,
    includeAdjacent: true,
    selectedText: "crosses the hall",
});
assert.equal(context.scene_id, "hall");
assert.equal(context.scene_index, 1);
assert.equal(context.shared_prompt, "Keep <Picture 1> identity and the red coat.");
assert.match(context.previous_prompt, /reaches the door/);
assert.match(context.next_prompt, /already-opening door/);
assert.equal(context.selected_text, "crosses the hall");
assert.equal(context.duration_seconds, 10);

const revision = promptSourceRevision("hall", source);
assert.equal(revision, promptSourceRevision("hall", source));
assert.notEqual(revision, promptSourceRevision("hall", `${source}!`));
const draft = {
    sceneId: "hall",
    sceneIndex: 1,
    sourceRevision: revision,
    proposed: "A concrete replacement.",
};
assert.deepEqual(draftConflict(draft, "hall", source), {stale: false, reason: ""});
assert.equal(draftConflict(draft, "hall", `${source}!`).stale, true);
assert.equal(draftConflict(draft, "street", source).stale, true);
assert.equal(promptSceneKey("hall", 1), "1:hall");

const request = makePromptAssistRequest({
    requestId: "pa-test",
    conversationId: "conversation-test",
    provider: "hermes",
    mode: "continuity",
    instruction: "",
    context,
});
assert.equal(request.type, "prompt_assist_request");
assert.equal(request.source_revision, revision);
assert.match(request.instruction, /continuity/i);

const fetchSecure = async (path) => ({
    ok: true,
    json: async () => path.endsWith("bridge_url")
        ? {url: "wss://bridge.example/?token=secret"}
        : {bridge_url: "ws://127.0.0.1:9180"},
});
assert.equal(await discoverPromptAssistBridge({
    fetchImpl: fetchSecure,
    locationLike: {protocol: "https:"},
    storage: null,
}), "wss://bridge.example/?token=secret");

assert.equal(await discoverPromptAssistBridge({
    fetchImpl: async () => ({ok: false}),
    locationLike: {protocol: "http:"},
    storage: {getItem: () => "ws://localhost:9911"},
}), "ws://localhost:9911");

assert.equal(await discoverPromptAssistBridge({
    fetchImpl: async (path) => ({
        ok: true,
        json: async () => path.endsWith("status")
            ? {bridge_url: "ws://127.0.0.1:9180"} : {url: null},
    }),
    locationLike: {protocol: "http:"},
    storage: {getItem: () => null},
}), "ws://127.0.0.1:9180");

const priorWebSocket = globalThis.WebSocket;
const sockets = [];
class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSED = 3;

    constructor(url) {
        this.url = url;
        this.readyState = FakeWebSocket.CONNECTING;
        this.handlers = new Map();
        this.sent = [];
        sockets.push(this);
        queueMicrotask(() => {
            this.readyState = FakeWebSocket.OPEN;
            this.emit("open", {});
        });
    }

    addEventListener(type, handler) {
        const handlers = this.handlers.get(type) || [];
        handlers.push(handler);
        this.handlers.set(type, handlers);
    }

    emit(type, event) {
        for (const handler of this.handlers.get(type) || []) handler(event);
    }

    send(text) {
        const frame = JSON.parse(text);
        this.sent.push(frame);
        if (frame.type === "hello") {
            queueMicrotask(() => this.emit("message", {
                data: JSON.stringify({type: "prompt_assist_ready", providers: []}),
            }));
        }
    }

    close() {
        this.readyState = FakeWebSocket.CLOSED;
        this.emit("close", {});
    }
}

globalThis.WebSocket = FakeWebSocket;
try {
    const client = new PromptAssistantClient({
        identityKey: "reset-test",
        discover: async () => "ws://bridge.test",
    });
    assert.equal(client.reset(), false);
    await client.send({
        type: "prompt_assist_request",
        request_id: "request-after-reset",
    });
    assert.deepEqual(
        sockets[0].sent.map((frame) => frame.type),
        ["hello", "prompt_assist_reset", "prompt_assist_request"],
    );
    client.close();
} finally {
    if (priorWebSocket === undefined) delete globalThis.WebSocket;
    else globalThis.WebSocket = priorWebSocket;
}

console.log("H3 Prompt Assistant core: context, revision fence, request, and bridge discovery pass");
