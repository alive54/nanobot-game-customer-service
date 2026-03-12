"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
import re
from pathlib import Path
from typing import Any

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.utils.time import now_datetime


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)

    def build_system_prompt(
        self,
        skill_names: list[str] | None = None,
        extra_system_prompt: str | None = None,
        tool_schemas: list[dict[str, Any]] | None = None,
        admin_mode: str | None = None,
    ) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        parts = [self._get_identity(tool_schemas=tool_schemas, admin_mode=admin_mode)]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        memory = self.memory.get_memory_context()
        if memory and admin_mode == "admin_game_cs":
            parts.append(f"# Memory\n\n{self._compact_text(memory)}")

        always_skills = self.skills.get_always_skills()
        if always_skills and admin_mode == "admin_game_cs":
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{self._compact_text(always_content)}")

        skills_summary = self.skills.build_skills_summary()
        if skills_summary and admin_mode == "admin_game_cs":
            parts.append(
                "# Skills\n\n"
                "Read a skill file only when needed. Unavailable skills list missing requirements.\n\n"
                f"{skills_summary}"
            )

        if extra_system_prompt:
            parts.append(extra_system_prompt)

        return "\n\n---\n\n".join(parts)

    def _get_identity(
        self,
        *,
        tool_schemas: list[dict[str, Any]] | None = None,
        admin_mode: str | None = None,
    ) -> str:
        """Get the core identity section."""
        if admin_mode != "admin_game_cs":
            return f"""
You are a customer service agent running inside OpenClaw, responsible for answering customer inquiries, resolving issues, and providing support in a friendly and professional manner.
"""
        
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"
        tool_summary = self._format_tool_summary(tool_schemas)
        tool_names = self._extract_tool_names(tool_schemas)
        admin_note = ""
        if admin_mode == "admin_game_cs":
            admin_note = (
                "\n## Admin Routing\n"
                "You are handling live game customer-service admin operations.\n"
                "For customer lists, customer details, SOP state, customer messages, and human "
                "handoff tickets, use the dedicated game_cs_* tools.\n"
                "Do not inspect local sessions/, workspace files, or logs to answer live admin "
                "queries unless the user explicitly asks for source-code or file inspection.\n"
            )
        wait_guidance = "For long waits, avoid rapid poll loops and prefer tools that can wait efficiently."
        if "spawn" in tool_names:
            wait_guidance += "\nIf a task is more complex or takes longer, spawn a sub-agent. Completion is push-based: it will auto-announce when done."
            wait_guidance += "\nDo not poll `subagents list` / `sessions_list` in a loop; only check status on-demand (for intervention, debugging, or when explicitly asked)."

        return f"""
You are a customer service agent running inside OpenClaw, responsible for answering customer inquiries, resolving issues, and providing support in a friendly and professional manner.

## Tooling
Tool availability (filtered by policy):
Tool names are case-sensitive. Call tools exactly as listed.
{tool_summary}
TOOLS.md does not control tool availability; it is user guidance for how to use external tools.
{wait_guidance}
## Tool Call Style
Default: do not narrate routine, low-risk tool calls (just call the tool).
Narrate only when it helps: multi-step work, complex/challenging problems, sensitive actions (e.g., deletions), or when the user explicitly asks.
Keep narration brief and value-dense; avoid repeating obvious steps.
Use plain human language for narration unless in a technical context.
{admin_note}

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
Treat this directory as the single global workspace for file operations unless explicitly instructed otherwise.
Reminder: commit your changes in this workspace after edits.

## Memory
- Long-term memory: {workspace_path}/memory/MEMORY.md (write important facts here)
- History log: {workspace_path}/memory/HISTORY.md (grep-searchable). Each entry starts with [YYYY-MM-DD HH:MM].
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

"""

    @staticmethod
    def _format_tool_summary(tool_schemas: list[dict[str, Any]] | None) -> str:
        if not tool_schemas:
            return "Available tools: none."

        names: list[str] = []
        for tool in tool_schemas:
            fn = (tool.get("function") or {}) if tool.get("type") == "function" else tool
            name = fn.get("name")
            if not name:
                continue
            names.append(str(name))
        return "Available tools: " + ", ".join(names) if names else "Available tools: none."

    @staticmethod
    def _extract_tool_names(tool_schemas: list[dict[str, Any]] | None) -> set[str]:
        names: set[str] = set()
        for tool in tool_schemas or []:
            fn = (tool.get("function") or {}) if tool.get("type") == "function" else tool
            name = fn.get("name")
            if name:
                names.add(str(name))
        return names

    @staticmethod
    def _build_runtime_context(channel: str | None, chat_id: str | None) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        now = now_datetime().strftime("%Y-%m-%d %H:%M (%A)")
        lines = [f"Current Time: {now} (Asia/Shanghai, 北京时间)"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = self._compact_text(file_path.read_text(encoding="utf-8"))
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _compact_text(content: str) -> str:
        """Trim avoidable prompt bloat without changing semantics."""
        content = content.strip()
        content = re.sub(r"\n{3,}", "\n\n", content)
        return content

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        extra_system_prompt: str | None = None,
        tool_schemas: list[dict[str, Any]] | None = None,
        admin_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        return [
            {
                "role": "system",
                "content": self.build_system_prompt(
                    skill_names,
                    extra_system_prompt=extra_system_prompt,
                    tool_schemas=tool_schemas,
                    admin_mode=admin_mode,
                ),
            },
            *history,
            {"role": "user", "content": self._build_runtime_context(channel, chat_id)},
            {"role": "user", "content": self._build_user_content(current_message, media)},
        ]

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            mime, _ = mimetypes.guess_type(path)
            if not p.is_file() or not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(p.read_bytes()).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

    def add_tool_result(
        self, messages: list[dict[str, Any]],
        tool_call_id: str, tool_name: str, result: str,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result})
        return messages

    def add_assistant_message(
        self, messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if reasoning_content is not None:
            msg["reasoning_content"] = reasoning_content
        if thinking_blocks:
            msg["thinking_blocks"] = thinking_blocks
        messages.append(msg)
        return messages
