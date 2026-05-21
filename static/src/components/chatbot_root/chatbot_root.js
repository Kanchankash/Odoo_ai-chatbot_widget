/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { FloatingIcon } from "../floating_icon/floating_icon";
import { ChatWindow } from "../chat_window/chat_window";

/**
 * Root component mounted into the web client action manager.
 * Renders both the FAB and the chat window.
 */
export class ChatbotRoot extends Component {
    static template = "ai_chatbot_widget.ChatbotRoot";
    static components = { FloatingIcon, ChatWindow };
    static props = {};

    setup() {
        this.chatbot = useService("aiChatbot");
        this.state = useState(this.chatbot.state);

        // Don't pre-load sessions on mount — open() fetches them lazily when the user clicks
    }
}

registry.category("main_components").add("ai_chatbot_widget.ChatbotRoot", {
    Component: ChatbotRoot,
    props: {},
});
