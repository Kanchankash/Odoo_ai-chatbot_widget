/** @odoo-module **/

import { Component, useRef, onMounted, onWillUnmount, onPatched } from "@odoo/owl";

/**
 * Wraps Chart.js v4 (loaded via chart.umd.js in static/lib/).
 * Destroys the Chart instance on unmount to avoid canvas memory leaks.
 */
export class ChartRenderer extends Component {
    static template = "ai_chatbot_widget.ChartRenderer";
    static props = {
        config: { type: Object },
    };

    setup() {
        this.canvasRef = useRef("canvas");
        this._chart = null;

        onMounted(() => this._initChart());
        onPatched(() => this._updateChart());
        onWillUnmount(() => this._destroyChart());
    }

    _initChart() {
        const canvas = this.canvasRef.el;
        if (!canvas) return;
        // Chart is loaded as a global from chart.umd.js
        if (typeof Chart === "undefined") {
            console.error("[ChartRenderer] Chart.js not loaded");
            return;
        }
        this._destroyChart();
        this._chart = new Chart(canvas, this.props.config);
    }

    _updateChart() {
        if (!this._chart) {
            this._initChart();
            return;
        }
        const cfg = this.props.config;
        if (!cfg) return;
        this._chart.data = cfg.data;
        this._chart.options = cfg.options || {};
        this._chart.update("none");
    }

    _destroyChart() {
        if (this._chart) {
            this._chart.destroy();
            this._chart = null;
        }
    }
}
