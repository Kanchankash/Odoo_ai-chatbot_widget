/** @odoo-module **/

import { Component, useState, useRef, onMounted } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

export class Composer extends Component {
    static template = "ai_chatbot_widget.Composer";
    static props = {};

    setup() {
        this.chatbot = useService("aiChatbot");
        this.state = useState(this.chatbot.state);
        this.localState = useState({ value: "" });
        this.textareaRef = useRef("textarea");

        onMounted(() => {
            if (this.textareaRef.el) {
                this.textareaRef.el.focus();
            }
        });
    }

    onInput(ev) {
        this.localState.value = ev.target.value;
        this._autoResize(ev.target);
    }

    _autoResize(el) {
        el.style.height = "auto";
        el.style.height = Math.min(el.scrollHeight, 120) + "px";
    }

    onKeydown(ev) {
        if (ev.key === "Enter" && !ev.shiftKey) {
            ev.preventDefault();
            this.send();
        }
    }

    async send() {
        const content = (this.localState.value || "").trim();
        if (!content) return;
        // If AI is still streaming, cancel it first then send the new message
        if (this.state.isStreaming) {
            this.chatbot.stopStreaming();
        }
        this.localState.value = "";
        if (this.textareaRef.el) {
            this.textareaRef.el.style.height = "auto";
        }
        await this.chatbot.sendMessage(content);
    }

    stop() {
        this.chatbot.stopStreaming();
    }
}
