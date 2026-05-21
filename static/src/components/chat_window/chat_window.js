/** @odoo-module **/

import { Component, useState, useRef, onMounted, onPatched } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { SidebarHistory } from "../sidebar_history/sidebar_history";
import { MessageBubble } from "../message_bubble/message_bubble";
import { Composer } from "../composer/composer";

export class ChatWindow extends Component {
    static template = "ai_chatbot_widget.ChatWindow";
    static components = { SidebarHistory, MessageBubble, Composer };
    static props = {};

    setup() {
        this.chatbot = useService("aiChatbot");
        this.state = useState(this.chatbot.state);
        this.messagesRef = useRef("messages");

        onMounted(() => this._scrollToBottom());
        onPatched(() => this._scrollToBottom());
    }

    _scrollToBottom() {
        const el = this.messagesRef.el;
        if (el) el.scrollTop = el.scrollHeight;
    }

    onMinimize() {
        if (this.state.isMinimized) {
            this.chatbot.restore();
        } else {
            this.chatbot.minimize();
        }
    }

    onToggleExpand() {
        if (this.state.isExpanded) {
            this.chatbot.collapse();
        } else {
            this.chatbot.expand();
        }
    }

    // Double-click header to toggle expand
    onHeaderDblClick() {
        if (!this.state.isMinimized) {
            this.onToggleExpand();
        }
    }

    onClose() {
        this.chatbot.close();
    }

    onNewChat() {
        this.chatbot.newSession();
    }
}
