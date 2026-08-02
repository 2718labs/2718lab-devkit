"""Inert text templates used only by the explicit scaffold command."""

TEMPLATES: dict[str, str] = {
    ".gitignore": """__pycache__/\n*.py[cod]\n.venv/\n.pytest_cache/\n.ruff_cache/\n""",
    "CHANGELOG.md": """# Changelog\n\n## [v0.1.0] - YYYY-MM-DD\n\n### Added\n\n- Initial plugin scaffold.\n""",
    "README.md": """# {{display_name}}\n\n{{description}}\n\n- AstrBot version: `>=4.16,<5`\n- Plugin name: `{{plugin_name}}`\n\n## Install\n\nPlace this directory in `AstrBot/data/plugins/` and reload plugins from the\nAstrBot WebUI. Replace the placeholder metadata before publishing.\n\n## Command\n\n- `/{{command}}`: returns a minimal response.\n""",
    "main.py": """\"\"\"{{plugin_name}} - {{description}}\"\"\"\n\nfrom astrbot.api.event import AstrMessageEvent, filter\nfrom astrbot.api.star import Context, Star\n\n\nclass {{class_name}}(Star):\n    \"\"\"Minimal plugin skeleton with no initialization side effects.\"\"\"\n\n    def __init__(self, context: Context):\n        super().__init__(context)\n\n    @filter.command(\"{{command}}\")\n    async def {{handler_name}}(self, event: AstrMessageEvent):\n        \"\"\"Return a minimal response.\"\"\"\n        yield event.plain_result(\"{{display_name}} is ready.\")\n\n    async def terminate(self):\n        \"\"\"Release resources if this template later acquires any.\"\"\"\n        return None\n""",
    "metadata.yaml": """name: {{plugin_name}}\ndisplay_name: {{display_name_yaml}}\ndesc: {{description_yaml}}\nversion: v0.1.0\nauthor: {{author_yaml}}\nrepo: {{repo_yaml}}\nastrbot_version: \">=4.16,<5\"\n""",
}
