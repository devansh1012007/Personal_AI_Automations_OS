"""Slash commands for the interactive agent.

Clean-room implementation of the slash-command capability: ``/name args`` typed
by the user is intercepted before it reaches the model and dispatched to a
handler. Two kinds coexist:

* **Built-in** commands, registered in code (``/help``, ``/model``, ``/mode``…).
* **Custom** commands, defined as markdown files in a directory — the file body
  is a prompt template with ``$ARGUMENTS`` / ``$1`` substitution, so a user
  grows their own commands without writing code. This is the mechanism that
  makes the agent extensible by its operator, not just by its authors.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import structlog

__all__ = ["CommandRegistry", "CommandResult", "SlashCommand", "parse_command_line"]

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class CommandResult:
    """What running a command produced."""

    handled: bool
    #: Text to show the user directly (a built-in that just prints).
    output: str | None = None
    #: A prompt to feed the model instead of the raw input (custom commands).
    prompt: str | None = None
    #: A side effect the loop should apply (e.g. {"set_mode": "plan"}).
    action: dict[str, str] = field(default_factory=dict)


CommandHandler = Callable[[str], Awaitable[CommandResult] | CommandResult]


@dataclass(slots=True)
class SlashCommand:
    name: str
    description: str
    handler: CommandHandler | None = None
    prompt_template: str | None = None
    source: str = "builtin"


def parse_command_line(text: str) -> tuple[str, str] | None:
    """Split ``/name rest`` into ``(name, rest)``, or None if not a command.

    A leading ``/`` with a name is the trigger; anything else is a normal
    prompt and returns None so the loop passes it through untouched.
    """
    text = text.strip()
    if not text.startswith("/") or len(text) < 2:
        return None
    match = re.match(r"^/([a-zA-Z0-9_\-:]+)\s*(.*)$", text, re.DOTALL)
    if not match:
        return None
    return match.group(1), match.group(2).strip()


class CommandRegistry:
    """Registers and dispatches slash commands."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(self, command: SlashCommand) -> None:
        self._commands[command.name] = command

    def register_builtin(
        self, name: str, description: str, handler: CommandHandler
    ) -> None:
        self.register(SlashCommand(name=name, description=description, handler=handler))

    def get(self, name: str) -> SlashCommand | None:
        return self._commands.get(name)

    def names(self) -> list[str]:
        return sorted(self._commands)

    def load_markdown_commands(self, directory: Path | str) -> int:
        """Load ``*.md`` files from a directory as custom commands.

        The filename (without extension) is the command name; the body is the
        prompt template. Returns how many were loaded. A missing directory is
        not an error — a fresh install simply has no custom commands.
        """
        path = Path(directory)
        if not path.exists():
            return 0
        loaded = 0
        for md in sorted(path.glob("*.md")):
            name = md.stem
            body = md.read_text(encoding="utf-8")
            description = _first_line(body) or f"custom command {name}"
            self.register(
                SlashCommand(
                    name=name,
                    description=description,
                    prompt_template=body,
                    source=f"file:{md.name}",
                )
            )
            loaded += 1
        return loaded

    async def dispatch(self, text: str) -> CommandResult:
        """Run the command in ``text``, or return handled=False if it isn't one."""
        parsed = parse_command_line(text)
        if parsed is None:
            return CommandResult(handled=False)
        name, args = parsed
        command = self._commands.get(name)
        if command is None:
            return CommandResult(
                handled=True, output=f"Unknown command: /{name}. Try /help."
            )

        if command.prompt_template is not None:
            expanded = _expand_template(command.prompt_template, args)
            return CommandResult(handled=True, prompt=expanded)

        if command.handler is not None:
            result = command.handler(args)
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[misc]
            return result  # type: ignore[return-value]

        return CommandResult(handled=True, output=f"/{name} has no handler.")


def _expand_template(template: str, args: str) -> str:
    """Substitute ``$ARGUMENTS`` and positional ``$1..$9`` into a template."""
    parts = args.split()
    out = template.replace("$ARGUMENTS", args)
    for i in range(9, 0, -1):
        out = out.replace(f"${i}", parts[i - 1] if i <= len(parts) else "")
    return out


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped
    return ""


def default_registry(*, on_help: str = "") -> CommandRegistry:
    """A registry pre-loaded with the common built-ins.

    Handlers here are pure (they return an action for the loop to apply) so the
    registry has no dependency on the loop — the loop reads ``result.action``
    and does the mutation. That keeps commands testable in isolation.
    """
    reg = CommandRegistry()

    def _help(_: str) -> CommandResult:
        lines = []
        for name in reg.names():
            command = reg.get(name)
            if command is not None:
                lines.append(f"/{command.name} — {command.description}")
        return CommandResult(handled=True, output=on_help + "\n".join(lines))

    def _action(key: str, value: str = "1") -> CommandHandler:
        return lambda _: CommandResult(handled=True, action={key: value})

    def _model(arg: str) -> CommandResult:
        if not arg:
            return CommandResult(handled=True, output="Usage: /model <name>")
        return CommandResult(handled=True, action={"set_model": arg})

    def _mode(arg: str) -> CommandResult:
        return CommandResult(handled=True, action={"set_mode": arg} if arg else {})

    reg.register_builtin("help", "List available commands", _help)
    reg.register_builtin("clear", "Clear the conversation transcript", _action("clear"))
    reg.register_builtin("model", "Show or set the active model", _model)
    reg.register_builtin("mode", "Show or set the permission mode", _mode)
    reg.register_builtin("compact", "Compact the transcript to free context", _action("compact"))
    reg.register_builtin("cost", "Show token/cost usage this session", _action("show_cost"))
    return reg
