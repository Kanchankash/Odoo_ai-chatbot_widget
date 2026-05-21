{
    "name": "AI Chatbot Widget",
    "version": "19.0.1.0.0",
    "category": "Productivity",
    "summary": "Floating AI chat assistant powered by OpenAI-compatible LLMs",
    "license": "LGPL-3",
    "depends": ["base", "web", "mail"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/ai_chatbot_security.xml",
        "security/ir.model.access.csv",
        "views/res_config_settings_views.xml",
        "views/chat_session_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "ai_chatbot_widget/static/lib/chart.umd.js",
            "ai_chatbot_widget/static/src/services/chat_service.js",
            "ai_chatbot_widget/static/src/components/**/*.js",
            "ai_chatbot_widget/static/src/components/**/*.xml",
            "ai_chatbot_widget/static/src/components/**/*.scss",
            "ai_chatbot_widget/static/src/systray/chatbot_systray.js",
        ],
    },
    "installable": True,
    "application": False,
}
