/** @odoo-module **/

import { Component, useState, markup } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { ChartRenderer } from "../chart_renderer/chart_renderer";

export class MessageBubble extends Component {
    static template = "ai_chatbot_widget.MessageBubble";
    static components = { ChartRenderer };
    static props = { message: { type: Object } };

    setup() {
        this.chatbot = useService("aiChatbot");
        this.state = useState(this.chatbot.state);
    }

    get message() { return this.props.message; }
    get isUser() { return this.props.message.role === "user"; }
    get isAssistant() { return this.props.message.role === "assistant"; }
    get isStreaming() { return !!this.props.message.isStreaming; }

    get chartSpec() {
        const spec = this.props.message.chart_spec;
        if (!spec) return null;
        try { return typeof spec === "string" ? JSON.parse(spec) : spec; }
        catch (_) { return null; }
    }

    get renderedContent() {
        const raw = this.props.message.content || "";
        if (!raw) return markup("");

        let html = _escapeHtml(raw);

        // Protect fenced code blocks from line processing
        const saved = [];
        html = html.replace(/```[\w]*\n?([\s\S]*?)```/g, (_, code) => {
            const ph = `\x00CB${saved.length}\x00`;
            saved.push(`<pre class="o_chatbot_code_block"><code>${code.trim()}</code></pre>`);
            return ph;
        });

        // Process line-by-line (handles tables not separated by blank lines)
        const lines = html.split("\n");
        const out = [];
        let i = 0;

        while (i < lines.length) {
            const line = lines[i];
            const trimmed = line.trim();

            // blank line — skip
            if (!trimmed) { i++; continue; }

            // Markdown table: current line starts with | and next is a separator row
            if (
                trimmed.startsWith("|") &&
                i + 1 < lines.length &&
                lines[i + 1].trim().match(/^\|[\s\-:|]+\|/)
            ) {
                const tableLines = [];
                while (i < lines.length && lines[i].trim().startsWith("|")) {
                    tableLines.push(lines[i]);
                    i++;
                }
                out.push(_renderTable(tableLines));
                continue;
            }

            // Heading
            const h3m = trimmed.match(/^###\s+(.+)/);
            const h2m = trimmed.match(/^##\s+(.+)/);
            const h1m = trimmed.match(/^#\s+(.+)/);
            if (h3m) { out.push(`<h4 class="o_chatbot_heading">${_inline(h3m[1])}</h4>`); i++; continue; }
            if (h2m) { out.push(`<h3 class="o_chatbot_heading">${_inline(h2m[1])}</h3>`); i++; continue; }
            if (h1m) { out.push(`<h2 class="o_chatbot_heading">${_inline(h1m[1])}</h2>`); i++; continue; }

            // Bullet list — collect consecutive bullet lines
            if (trimmed.match(/^[-*]\s+/)) {
                const items = [];
                while (i < lines.length && lines[i].trim().match(/^[-*]\s+/)) {
                    items.push(`<li>${_inline(lines[i].trim().replace(/^[-*]\s+/, ""))}</li>`);
                    i++;
                }
                out.push(`<ul class="o_chatbot_list">${items.join("")}</ul>`);
                continue;
            }

            // Paragraph — collect until blank line, table, heading, or list
            const para = [];
            while (i < lines.length) {
                const t = lines[i].trim();
                if (!t) { i++; break; }
                if (t.startsWith("|") || t.match(/^#{1,3}\s/) || t.match(/^[-*]\s+/)) break;
                para.push(_inline(lines[i]));
                i++;
            }
            if (para.length) out.push(`<p>${para.join("<br>")}</p>`);
        }

        // Restore code blocks
        let result = out.join("");
        saved.forEach((code, idx) => { result = result.replace(`\x00CB${idx}\x00`, code); });

        return markup(result);
    }
}

function _inline(text) {
    return text
        .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
        .replace(/\*([^*\n]+)\*/g, "<em>$1</em>")
        .replace(/`([^`\n]+)`/g, "<code>$1</code>");
}

function _renderTable(lines) {
    const parseCells = (line) =>
        line.split("|").slice(1, -1).map((c) => c.trim());

    const headers = parseCells(lines[0]);
    const dataLines = lines.slice(2).filter((l) => l.trim());
    const rows = dataLines.map(parseCells);

    const th = headers.map((h) => `<th>${_inline(h)}</th>`).join("");
    const trs = rows
        .map((row) => `<tr>${row.map((c) => `<td>${_inline(c)}</td>`).join("")}</tr>`)
        .join("");

    return `<div class="o_chatbot_table_wrap"><table class="o_chatbot_table"><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table></div>`;
}

function _escapeHtml(str) {
    return str
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}
