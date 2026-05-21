/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

export class SidebarHistory extends Component {
    static template = "ai_chatbot_widget.SidebarHistory";
    static props = {};

    setup() {
        this.chatbot = useService("aiChatbot");
        this.state = useState(this.chatbot.state);
        this.localState = useState({ renamingId: null, renameValue: "" });
    }

    onSelectSession(sessionId) {
        if (sessionId !== this.state.currentSessionId) {
            this.chatbot.openSession(sessionId);
        }
    }

    onNewChat() {
        this.chatbot.newSession();
    }

    startRename(session, ev) {
        ev.stopPropagation();
        this.localState.renamingId = session.id;
        this.localState.renameValue = session.name;
    }

    async confirmRename(sessionId, ev) {
        ev.stopPropagation();
        await this.chatbot.renameSession(sessionId, this.localState.renameValue);
        this.localState.renamingId = null;
    }

    onRenameKeydown(sessionId, ev) {
        if (ev.key === "Enter") {
            this.confirmRename(sessionId, ev);
        } else if (ev.key === "Escape") {
            this.localState.renamingId = null;
        }
    }

    async onArchive(sessionId, ev) {
        ev.stopPropagation();
        await this.chatbot.archiveSession(sessionId);
    }
}
