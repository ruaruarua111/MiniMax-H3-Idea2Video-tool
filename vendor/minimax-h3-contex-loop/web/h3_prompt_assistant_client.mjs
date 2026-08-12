const DEFAULT_BRIDGE_URL = "ws://127.0.0.1:9180";
const PANEL_BRIDGE_STORAGE_KEY = "comfyui-mcp.panel.bridgeUrl";

function storedBridgeUrl(storage) {
    try {
        const value = storage?.getItem?.(PANEL_BRIDGE_STORAGE_KEY);
        return typeof value === "string" && /^wss?:\/\//.test(value.trim())
            ? value.trim() : null;
    } catch (_error) {
        return null;
    }
}

async function jsonFrom(fetchImpl, path) {
    try {
        const response = await fetchImpl(path, {cache: "no-store"});
        if (!response.ok) return null;
        return await response.json();
    } catch (_error) {
        return null;
    }
}

export async function discoverPromptAssistBridge({
    fetchImpl = globalThis.fetch?.bind(globalThis),
    locationLike = globalThis.location,
    storage = globalThis.localStorage,
} = {}) {
    if (locationLike?.protocol === "https:" && fetchImpl) {
        const advertised = await jsonFrom(fetchImpl, "/comfyui_mcp_panel/bridge_url");
        if (typeof advertised?.url === "string" && advertised.url.startsWith("wss://")) {
            return advertised.url;
        }
    }
    const stored = storedBridgeUrl(storage);
    if (stored) return stored;
    if (fetchImpl) {
        const status = await jsonFrom(fetchImpl, "/comfyui_mcp_panel/status");
        if (typeof status?.bridge_url === "string" && /^wss?:\/\//.test(status.bridge_url)) {
            return status.bridge_url;
        }
    }
    return DEFAULT_BRIDGE_URL;
}

function randomId() {
    return globalThis.crypto?.randomUUID?.()
        ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function sessionIdentity(key) {
    const storageKey = `h3.prompt-assistant.client.${key}`;
    try {
        let value = globalThis.sessionStorage?.getItem(storageKey);
        if (!value) {
            value = randomId();
            globalThis.sessionStorage?.setItem(storageKey, value);
        }
        return value;
    } catch (_error) {
        return randomId();
    }
}

export class PromptAssistantClient {
    constructor({identityKey, onFrame, onStatus, discover = discoverPromptAssistBridge}) {
        this.identity = sessionIdentity(identityKey || randomId());
        this.routeId = `prompt-assistant:${this.identity}`;
        this.conversationId = `h3-${this.identity}`;
        this.onFrame = onFrame;
        this.onStatus = onStatus;
        this.discover = discover;
        this.socket = null;
        this.connecting = null;
        this.closed = false;
        this.needsReset = false;
    }

    status(value, detail) {
        this.onStatus?.(value, detail);
    }

    async connect() {
        if (this.closed) throw new Error("Prompt assistant client is closed.");
        if (this.socket?.readyState === WebSocket.OPEN) return;
        if (this.connecting) return this.connecting;
        this.status("connecting");
        this.connecting = (async () => {
            const url = await this.discover();
            await new Promise((resolve, reject) => {
                let ready = false;
                const socket = new WebSocket(url);
                this.socket = socket;
                const timer = globalThis.setTimeout(() => {
                    if (ready) return;
                    try { socket.close(); } catch (_error) { /* already closed */ }
                    reject(new Error("The comfyui-mcp prompt-assist handshake timed out."));
                }, 15_000);
                socket.addEventListener("open", () => {
                    socket.send(JSON.stringify({
                        type: "hello",
                        tab_id: this.routeId,
                        tab_session_id: this.identity,
                        title: "H3 Prompt Assistant",
                        headless: true,
                        client_kind: "prompt_assistant",
                    }));
                });
                socket.addEventListener("message", (event) => {
                    let frame;
                    try { frame = JSON.parse(String(event.data ?? "")); } catch (_error) { return; }
                    if (frame?.type === "prompt_assist_ready") {
                        ready = true;
                        globalThis.clearTimeout(timer);
                        if (this.needsReset) {
                            socket.send(JSON.stringify({
                                tab_id: this.routeId,
                                type: "prompt_assist_reset",
                                conversation_id: this.conversationId,
                            }));
                            this.needsReset = false;
                        }
                        this.status("connected", frame);
                        resolve();
                    }
                    this.onFrame?.(frame);
                });
                socket.addEventListener("error", () => {
                    if (!ready) {
                        globalThis.clearTimeout(timer);
                        try { socket.close(); } catch (_error) { /* already closed */ }
                        reject(new Error(`Could not connect to the comfyui-mcp bridge at ${url.split("?")[0]}.`));
                    }
                });
                socket.addEventListener("close", () => {
                    globalThis.clearTimeout(timer);
                    if (this.socket === socket) this.socket = null;
                    this.status("disconnected");
                    if (!ready) reject(new Error("The comfyui-mcp bridge closed before prompt-assist was ready."));
                });
            });
        })().finally(() => {
            this.connecting = null;
        });
        return this.connecting;
    }

    async send(frame) {
        await this.connect();
        if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
            throw new Error("Prompt assistant is not connected.");
        }
        this.socket.send(JSON.stringify({tab_id: this.routeId, ...frame}));
    }

    cancel(requestId) {
        if (this.socket?.readyState !== WebSocket.OPEN) return false;
        this.socket.send(JSON.stringify({
            tab_id: this.routeId,
            type: "prompt_assist_cancel",
            request_id: requestId,
        }));
        return true;
    }

    reset() {
        this.needsReset = true;
        if (this.socket?.readyState !== WebSocket.OPEN) return false;
        this.socket.send(JSON.stringify({
            tab_id: this.routeId,
            type: "prompt_assist_reset",
            conversation_id: this.conversationId,
        }));
        this.needsReset = false;
        return true;
    }

    close() {
        this.closed = true;
        if (this.socket?.readyState === WebSocket.OPEN) {
            try {
                this.socket.send(JSON.stringify({tab_id: this.routeId, type: "prompt_assist_close"}));
            } catch (_error) {
                // Socket is already going away.
            }
        }
        try { this.socket?.close(); } catch (_error) { /* already closed */ }
        this.socket = null;
    }
}
