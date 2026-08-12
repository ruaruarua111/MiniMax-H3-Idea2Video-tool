// Canvas-wide compatibility helper for ComfyUI_frontend #12443.
//
// Adapted with permission from pekkAi-dev/ComfyUI-LegacyWidgetWidthFix. The
// controller form lets this pack activate the workaround while one of its H3
// nodes is present, without adding a dedicated workflow node.

export const LEGACY_WIDTH_KEY = "__lwwf_width";
export const LEGACY_GUARD_KEY = "__lwwf_guarded";

const OWNER_KEY = "__h3_lwwf_owner";
const HOOK_KEY = "__h3_lwwf_hook";
const HOOK_METHODS = ["addWidget", "addDOMWidget", "addCustomWidget"];

export function graphNodes(graph, output = [], seen = new Set()) {
    if (!graph || seen.has(graph)) return output;
    seen.add(graph);
    for (const node of graph._nodes ?? []) {
        output.push(node);
        if (node?.subgraph) graphNodes(node.subgraph, output, seen);
    }
    return output;
}

export function createLegacyWidgetWidthController({
    getGraph,
    getLiteGraph = () => globalThis.LiteGraph,
    requestFrame = (callback) => globalThis.requestAnimationFrame?.(callback),
    onRelinquish = () => {},
} = {}) {
    const owner = {};
    const hosts = new Set();
    const hookedMethods = new WeakMap();
    let allowed = true;
    let active = false;
    let repairQueued = false;

    function inVueMode() {
        return !!getLiteGraph?.()?.vueNodesMode;
    }

    function ownsWidget(widget) {
        const descriptor = Object.getOwnPropertyDescriptor(widget ?? {}, "width");
        return descriptor?.set?.[OWNER_KEY] === owner;
    }

    function guardWidget(widget) {
        if (!active || !widget) return false;
        if (ownsWidget(widget)) return true;

        let width;
        try {
            width = Object.prototype.hasOwnProperty.call(widget, LEGACY_WIDTH_KEY)
                ? widget[LEGACY_WIDTH_KEY]
                : widget.width;
            widget[LEGACY_WIDTH_KEY] = width;

            const setter = function setLegacyWidgetWidth(value) {
                if (!active || inVueMode()) this[LEGACY_WIDTH_KEY] = value;
            };
            Object.defineProperty(setter, OWNER_KEY, {value: owner});
            Object.defineProperty(widget, "width", {
                configurable: true,
                enumerable: true,
                get() {
                    return this[LEGACY_WIDTH_KEY];
                },
                set: setter,
            });
            if (!widget[LEGACY_GUARD_KEY]) {
                Object.defineProperty(widget, LEGACY_GUARD_KEY, {
                    value: true,
                    configurable: true,
                    enumerable: false,
                });
            }
            return true;
        } catch (_error) {
            return false;
        }
    }

    function repairWidget(widget) {
        if (!active || inVueMode() || !guardWidget(widget)) return false;
        widget[LEGACY_WIDTH_KEY] = undefined;
        return true;
    }

    function unguardWidget(widget) {
        if (!ownsWidget(widget)) return false;
        const width = widget[LEGACY_WIDTH_KEY];
        try {
            delete widget.width;
            delete widget[LEGACY_WIDTH_KEY];
            delete widget[LEGACY_GUARD_KEY];
            widget.width = width;
            return true;
        } catch (_error) {
            return false;
        }
    }

    function eachWidget(callback) {
        for (const node of graphNodes(getGraph?.())) {
            for (const widget of node?.widgets ?? []) callback(widget, node);
        }
    }

    function dirtyCanvas() {
        getGraph?.()?.setDirtyCanvas?.(true, true);
    }

    function repairNode(node) {
        if (!active) return;
        for (const widget of node?.widgets ?? []) repairWidget(widget);
    }

    function repairAll() {
        if (!active) return;
        eachWidget((widget) => repairWidget(widget));
        dirtyCanvas();
    }

    function scheduleRepairAll() {
        if (!active || repairQueued) return;
        repairQueued = true;
        const callback = () => {
            repairQueued = false;
            repairAll();
        };
        if (typeof requestFrame === "function") requestFrame(callback);
        else globalThis.setTimeout?.(callback, 0);
    }

    function nodeCreated(node) {
        if (!active) return;
        repairNode(node);
        const callback = () => {
            if (active && node?.graph) repairNode(node);
        };
        if (typeof requestFrame === "function") requestFrame(callback);
        else globalThis.setTimeout?.(callback, 0);
    }

    function updateActive() {
        const next = allowed && hosts.size > 0;
        if (next === active) return;
        active = next;
        if (active) {
            repairAll();
            scheduleRepairAll();
            return;
        }
        eachWidget((widget) => unguardWidget(widget));
        dirtyCanvas();
        onRelinquish?.();
    }

    function addHost(node) {
        if (!node || hosts.has(node)) return;
        hosts.add(node);
        updateActive();
    }

    function removeHost(node) {
        if (!hosts.delete(node)) return;
        updateActive();
    }

    function syncHosts(nodes, isHost) {
        hosts.clear();
        for (const node of nodes ?? []) {
            if (isHost(node)) hosts.add(node);
        }
        updateActive();
    }

    function setAllowed(value) {
        allowed = value !== false;
        updateActive();
    }

    function installHooks(prototype) {
        if (!prototype) return;
        let installed = hookedMethods.get(prototype);
        if (!installed) {
            installed = new Map();
            hookedMethods.set(prototype, installed);
        }
        for (const name of HOOK_METHODS) {
            const original = prototype[name];
            if (typeof original !== "function") continue;
            if (original === installed.get(name) || original[HOOK_KEY] === owner) {
                continue;
            }
            const patched = function h3LegacyWidthPatchedWidgetMethod(...args) {
                const widget = original.apply(this, args);
                if (active && widget) repairWidget(widget);
                return widget;
            };
            Object.defineProperty(patched, HOOK_KEY, {value: owner});
            prototype[name] = patched;
            installed.set(name, patched);
        }
    }

    return {
        addHost,
        installHooks,
        nodeCreated,
        ownsWidget,
        removeHost,
        repairAll,
        repairNode,
        scheduleRepairAll,
        setAllowed,
        syncHosts,
        get active() {
            return active;
        },
        get allowed() {
            return allowed;
        },
        get hostCount() {
            return hosts.size;
        },
    };
}
