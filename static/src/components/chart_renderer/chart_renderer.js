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

    /**
     * Replace string callback markers with real JS functions.
     * Python can't embed functions in JSON, so we use sentinel strings.
     */
    _resolveCallbacks(options) {
        if (!options) return;
        const yTicks = options?.scales?.y?.ticks;
        if (yTicks && yTicks.callback === "formatAmount") {
            yTicks.callback = (value) => {
                const abs = Math.abs(value);
                if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
                if (abs >= 1_000)     return `${(value / 1_000).toFixed(0)}K`;
                return value.toLocaleString();
            };
        }
    }

    _buildConfig() {
        // Deep-clone so we never mutate the reactive props object
        const cfg = JSON.parse(JSON.stringify(this.props.config));
        this._resolveCallbacks(cfg.options);

        // Apply sensible defaults that improve readability
        const type = cfg.type;
        cfg.options = cfg.options || {};
        cfg.options.maintainAspectRatio = true;

        // Line chart enhancements: smooth curves, visible points, subtle fill
        if (type === "line") {
            (cfg.data?.datasets || []).forEach((ds) => {
                ds.tension      = ds.tension      ?? 0.35;
                ds.pointRadius  = ds.pointRadius  ?? 5;
                ds.pointHoverRadius = ds.pointHoverRadius ?? 8;
                ds.borderWidth  = ds.borderWidth  ?? 2;
                // Lighten fill for area effect
                if (ds.backgroundColor && ds.backgroundColor.length === 9) {
                    ds.backgroundColor = ds.backgroundColor.slice(0, 7) + "22";
                }
                ds.fill = true;
            });
        }

        // Bar chart: slightly rounded corners
        if (type === "bar") {
            cfg.options.borderRadius = cfg.options.borderRadius ?? 4;
        }

        // Cleaner grid: subtle horizontal lines, no vertical lines
        const scaleY = cfg.options?.scales?.y;
        if (scaleY) {
            scaleY.grid = scaleY.grid || {};
            scaleY.grid.color = "rgba(0,0,0,0.06)";
            scaleY.ticks = scaleY.ticks || {};
            scaleY.ticks.font = { size: 11 };
        }
        const scaleX = cfg.options?.scales?.x;
        if (scaleX) {
            scaleX.grid = { display: false };
            scaleX.ticks = scaleX.ticks || {};
            scaleX.ticks.font = { size: 11 };
        }

        // Tooltip: show all series at the hovered x-position
        cfg.options.plugins = cfg.options.plugins || {};
        cfg.options.plugins.tooltip = {
            mode: "index",
            intersect: false,
            callbacks: {
                label: (ctx) => {
                    const v = ctx.parsed.y;
                    const abs = Math.abs(v);
                    let formatted;
                    if (abs >= 1_000_000) formatted = `${(v / 1_000_000).toFixed(2)}M`;
                    else if (abs >= 1_000) formatted = `${(v / 1_000).toFixed(1)}K`;
                    else formatted = v.toLocaleString();
                    return ` ${ctx.dataset.label}: ${formatted}`;
                },
            },
        };

        return cfg;
    }

    _initChart() {
        const canvas = this.canvasRef.el;
        if (!canvas) return;
        if (typeof Chart === "undefined") {
            console.error("[ChartRenderer] Chart.js not loaded");
            return;
        }
        this._destroyChart();
        this._chart = new Chart(canvas, this._buildConfig());
    }

    _updateChart() {
        if (!this._chart) {
            this._initChart();
            return;
        }
        const cfg = this._buildConfig();
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
