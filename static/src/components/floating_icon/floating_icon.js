/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

export class FloatingIcon extends Component {
    static template = "ai_chatbot_widget.FloatingIcon";
    static props = {};

    setup() {
        this.chatbot = useService("aiChatbot");
        this.state = useState(this.chatbot.state);
    }

    onClick() {
        this.chatbot.toggle();
    }
}
