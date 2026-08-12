import {app} from "/scripts/app.js";
import {
    CORE_REF2VA_TYPE,
    VIDEO_REF_TYPE,
    convertCoreRef2VA,
    migrateLegacyVideoScheduleWidgets,
} from "./h3_reference_autoconnect_core.mjs";

const EXTENSION = "minimax_h3_context_loop.reference_autoconnect";

function notify(message, severity = "info") {
    const toast = app.extensionManager?.toast;
    if (typeof toast?.add === "function") {
        toast.add({severity, summary: "MiniMax H3", detail: message, life: 7000});
    } else {
        const logger = severity === "error" ? console.error : console.info;
        logger(`[MiniMax H3] ${message}`);
    }
}

function convert(node) {
    try {
        const result = convertCoreRef2VA(node, {
            createNode: (type) => globalThis.LiteGraph?.createNode?.(type),
        });
        app.canvas?.selectNode?.(result.wrapper);
        app.canvas?.setDirty?.(true, true);
        const notes = [];
        if (!result.connectedCurrent) notes.push("connect Current Shot clip index/count");
        if (!result.connectedFingerprint) notes.push(
            "connect the static schedule fingerprint to Plan when appropriate");
        if (result.ignoredOrphanVideoAudio) notes.push(
            `${result.ignoredOrphanVideoAudio} orphan video-audio socket was ignored`);
        notify(
            `Converted core Ref2VA into ${result.scheduleNodes.length} scheduled ` +
            `reference node${result.scheduleNodes.length === 1 ? "" : "s"}.` +
            (notes.length ? ` Check: ${notes.join("; ")}.` : ""),
        );
    } catch (error) {
        notify(error?.message ?? String(error), "error");
    }
}

app.registerExtension({
    name: EXTENSION,
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name === VIDEO_REF_TYPE) {
            const originalConfigure = nodeType.prototype.onConfigure;
            nodeType.prototype.onConfigure = function (info) {
                const result = originalConfigure?.apply(this, arguments);
                migrateLegacyVideoScheduleWidgets(this, info);
                return result;
            };
        }
        if (nodeData.name === CORE_REF2VA_TYPE) {
            const originalMenu = nodeType.prototype.getExtraMenuOptions;
            nodeType.prototype.getExtraMenuOptions = function (_, options) {
                const result = originalMenu?.apply(this, arguments);
                const menu = Array.isArray(options) ? options : [];
                menu.splice(Math.max(0, menu.length - 1), 0,
                    null,
                    {
                        content: "Convert to MiniMax H3 Scheduled Ref2VA",
                        callback: () => convert(this),
                    },
                );
                return result;
            };
        }
    },
});
