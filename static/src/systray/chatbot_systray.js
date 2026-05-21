/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

/**
 * Systray item that shows the chatbot toggle button in the top bar.
 * Clicking it delegates to the aiChatbot service.
 */
class ChatbotSystrayItem extends Component {
    static template = "ai_chatbot_widget.ChatbotSystrayItem";
    static props = {};

    setup() {
        this.chatbot = useService("aiChatbot");
        this.state = useState(this.chatbot.state);
    }

    onClick() {
        this.chatbot.toggle();
    }
}

registry.category("systray").add(
    "ai_chatbot_widget.chatbot_systray",
    { Component: ChatbotSystrayItem },
    { sequence: 100 }
);
