/** @odoo-module **/

import { reactive } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { browser } from "@web/core/browser/browser";

/**
 * aiChatbot service — shared reactive state + actions for all chatbot components.
 * Registered in the "services" registry under "aiChatbot".
 */

const _SESSION_STORAGE_KEY = "ai_chatbot_session_id";

function makeChatbotService({ env }) {
    const state = reactive({
        isOpen: false,
        isMinimized: false,
        isExpanded: false,
        isStreaming: false,
        hasUnread: false,
        currentSessionId: null,
        sessions: [],
        messages: [],
        streamingContent: "",
    });

    let abortController = null;
    let _opening = false;  // guard against concurrent open() calls

    // -----------------------------------------------------------------------
    // Window lifecycle
    // -----------------------------------------------------------------------

    async function toggle() {
        if (state.isOpen) {
            close();
        } else {
            await open();
        }
    }

    async function open() {
        if (_opening) return;  // prevent concurrent open() calls
        state.isOpen = true;
        state.isMinimized = false;
        state.hasUnread = false;
        if (!state.currentSessionId) {
            _opening = true;
            try {
                // Try to restore the last active session from localStorage
                const storedId = browser.localStorage.getItem(_SESSION_STORAGE_KEY);
                await loadSessions();

                // Find stored session in the list, or fall back to most recent
                let targetSession = null;
                if (storedId) {
                    const stored = state.sessions.find((s) => s.id === parseInt(storedId, 10));
                    if (stored) targetSession = stored;
                }
                if (!targetSession && state.sessions.length > 0) {
                    targetSession = state.sessions[0];
                }

                if (targetSession) {
                    await openSession(targetSession.id);
                } else {
                    await newSession();
                }
            } finally {
                _opening = false;
            }
        }
    }

    function close() {
        state.isOpen = false;
        state.isMinimized = false;
        state.isExpanded = false;
    }

    function minimize() {
        state.isMinimized = true;
        state.isExpanded = false;
    }

    function restore() {
        state.isMinimized = false;
        state.hasUnread = false;
    }

    function expand() {
        state.isExpanded = true;
        state.isMinimized = false;
    }

    function collapse() {
        state.isExpanded = false;
    }

    // -----------------------------------------------------------------------
    // Session management
    // -----------------------------------------------------------------------

    async function loadSessions() {
        try {
            const sessions = await rpc("/ai_chatbot/session/list", {});
            state.sessions = sessions || [];
        } catch (e) {
            console.error("[aiChatbot] loadSessions error:", e);
        }
    }

    async function newSession() {
        try {
            // Archive any empty sessions before creating a new one
            await _archiveEmptySessions();
            const result = await rpc("/ai_chatbot/session/new", {});
            if (result && result.session_id) {
                state.currentSessionId = result.session_id;
                browser.localStorage.setItem(_SESSION_STORAGE_KEY, String(result.session_id));
                state.messages = [];
                state.streamingContent = "";
                await loadSessions();
            }
        } catch (e) {
            console.error("[aiChatbot] newSession error:", e);
        }
    }

    async function _archiveEmptySessions() {
        // Archive sessions with no messages to prevent "New Chat" accumulation
        const emptySessions = state.sessions.filter((s) => s.message_count === 0);
        for (const s of emptySessions) {
            try {
                await rpc(`/ai_chatbot/session/${s.id}/archive`, {});
            } catch (_) {}
        }
    }

    async function openSession(sessionId) {
        try {
            const messages = await rpc(
                `/ai_chatbot/session/${sessionId}/messages`,
                {}
            );
            state.currentSessionId = sessionId;
            browser.localStorage.setItem(_SESSION_STORAGE_KEY, String(sessionId));
            state.messages = messages || [];
            state.streamingContent = "";
        } catch (e) {
            console.error("[aiChatbot] openSession error:", e);
        }
    }

    async function renameSession(sessionId, name) {
        try {
            const result = await rpc(
                `/ai_chatbot/session/${sessionId}/rename`,
                { name }
            );
            if (result && result.ok) {
                const sess = state.sessions.find((s) => s.id === sessionId);
                if (sess) sess.name = result.name;
            }
        } catch (e) {
            console.error("[aiChatbot] renameSession error:", e);
        }
    }

    async function archiveSession(sessionId) {
        try {
            await rpc(`/ai_chatbot/session/${sessionId}/archive`, {});
            state.sessions = state.sessions.filter((s) => s.id !== sessionId);
            if (state.currentSessionId === sessionId) {
                state.currentSessionId = null;
                browser.localStorage.removeItem(_SESSION_STORAGE_KEY);
                // Open next available session or create a fresh one
                if (state.sessions.length > 0) {
                    await openSession(state.sessions[0].id);
                } else {
                    await newSession();
                }
            }
        } catch (e) {
            console.error("[aiChatbot] archiveSession error:", e);
        }
    }

    // -----------------------------------------------------------------------
    // Messaging
    // -----------------------------------------------------------------------

    function _getCsrfToken() {
        // Odoo 19 embeds csrf_token as window.odoo.csrf_token (set in page HTML)
        return (window.odoo && window.odoo.csrf_token) || "";
    }

    async function sendMessage(content) {
        if (!content || !content.trim() || state.isStreaming) return;

        const sessionId = state.currentSessionId;
        if (!sessionId) return;

        // Add optimistic user message
        state.messages = [
            ...state.messages,
            { role: "user", content: content.trim(), id: Date.now() },
        ];
        state.isStreaming = true;
        state.streamingContent = "";

        // Placeholder assistant message (streaming)
        const streamingMsgId = "streaming_" + Date.now();
        state.messages = [
            ...state.messages,
            { role: "assistant", content: "", id: streamingMsgId, isStreaming: true },
        ];

        abortController = new AbortController();

        try {
            // Odoo 19: CSRF token must be in URL query params (not X-CSRFToken header)
            const csrfToken = _getCsrfToken();
            const url = `/ai_chatbot/chat/stream?csrf_token=${encodeURIComponent(csrfToken)}`;
            const response = await fetch(url, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({ session_id: sessionId, content }),
                signal: abortController.signal,
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = "";
            let finalMessageId = null;
            let chartSpec = null;

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split("\n");
                buffer = lines.pop(); // keep incomplete line

                let event = null;
                let data = null;

                for (const line of lines) {
                    if (line.startsWith("event:")) {
                        event = line.slice(6).trim();
                    } else if (line.startsWith("data:")) {
                        // SSE spec: multiple data: lines in one frame are joined with \n.
                        // Strip exactly one optional space after "data:" to preserve leading
                        // spaces in tokens (e.g. " how" carries a meaningful leading space).
                        const part = line.startsWith("data: ") ? line.slice(6) : line.slice(5);
                        data = data !== null ? data + "\n" + part : part;
                    } else if (line === "") {
                        // dispatch frame
                        if (event === "token" && data !== null) {
                            state.streamingContent += data;
                            // update streaming bubble
                            state.messages = state.messages.map((m) =>
                                m.id === streamingMsgId
                                    ? { ...m, content: state.streamingContent }
                                    : m
                            );
                        } else if (event === "done" && data) {
                            try {
                                const parsed = JSON.parse(data);
                                finalMessageId = parsed.message_id;
                                chartSpec = parsed.chart_spec || null;
                            } catch (_) {}
                        } else if (event === "error" && data) {
                            state.messages = state.messages.map((m) =>
                                m.id === streamingMsgId
                                    ? { ...m, content: `⚠ ${data}`, isError: true, isStreaming: false }
                                    : m
                            );
                        }
                        event = null;
                        data = null;
                    }
                }
            }

            // Finalize streaming message
            state.messages = state.messages.map((m) =>
                m.id === streamingMsgId
                    ? {
                          ...m,
                          id: finalMessageId || m.id,
                          content: state.streamingContent,
                          chart_spec: chartSpec,
                          isStreaming: false,
                      }
                    : m
            );

            // Unread dot if minimized
            if (state.isMinimized || !state.isOpen) {
                state.hasUnread = true;
            }

            await loadSessions();
        } catch (e) {
            if (e.name !== "AbortError") {
                console.error("[aiChatbot] sendMessage stream error:", e);
                state.messages = state.messages.map((m) =>
                    m.id === streamingMsgId
                        ? { ...m, content: "⚠ Connection error", isError: true, isStreaming: false }
                        : m
                );
            }
        } finally {
            state.isStreaming = false;
            state.streamingContent = "";
            abortController = null;
        }
    }

    function stopStreaming() {
        if (abortController) {
            abortController.abort();
            abortController = null;
        }
        state.isStreaming = false;
    }

    // -----------------------------------------------------------------------
    // Public API
    // -----------------------------------------------------------------------
    return {
        state,
        toggle,
        open,
        close,
        minimize,
        restore,
        expand,
        collapse,
        loadSessions,
        newSession,
        openSession,
        renameSession,
        archiveSession,
        sendMessage,
        stopStreaming,
    };
}

registry.category("services").add("aiChatbot", {
    start(env) {
        return makeChatbotService({ env });
    },
});
