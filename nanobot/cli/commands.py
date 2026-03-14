"""CLI commands for nanobot."""

import asyncio
import json
import os
import select
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from nanobot import __logo__, __version__
from nanobot.config.schema import Config
from nanobot.utils.helpers import sync_workspace_templates

app = typer.Typer(
    name="nanobot",
    help=f"{__logo__} nanobot - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    try:
        import termios

        termios.tcflush(fd, termios.TCIFLUSH)
        return
    except Exception:
        pass

    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break
    except Exception:
        return


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios

        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    try:
        import termios

        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    history_file = Path.home() / ".nanobot" / "history" / "cli_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,  # Enter submits (single line mode)
    )


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """Render assistant response with consistent terminal styling."""
    content = response or ""
    body = Markdown(content) if render_markdown else Text(content)
    console.print()
    console.print(f"[cyan]{__logo__} nanobot[/cyan]")
    console.print(body)
    console.print()


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} nanobot v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(None, "--version", "-v", callback=version_callback, is_eager=True),
):
    """nanobot - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard():
    """Initialize nanobot configuration and workspace."""
    from nanobot.config.loader import get_config_path, load_config, save_config
    from nanobot.config.schema import Config
    from nanobot.utils.helpers import get_workspace_path

    config_path = get_config_path()

    if config_path.exists():
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        console.print("  [bold]y[/bold] = overwrite with defaults (existing values will be lost)")
        console.print(
            "  [bold]N[/bold] = refresh config, keeping existing values and adding new fields"
        )
        if typer.confirm("Overwrite?"):
            config = Config()
            save_config(config)
            console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
        else:
            config = load_config()
            save_config(config)
            console.print(
                f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)"
            )
    else:
        save_config(Config())
        console.print(f"[green]✓[/green] Created config at {config_path}")

    # Create workspace
    workspace = get_workspace_path()

    if not workspace.exists():
        workspace.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace}")

    sync_workspace_templates(workspace)

    console.print(f"\n{__logo__} nanobot is ready!")
    console.print("\nNext steps:")
    console.print("  1. Add your API key to [cyan]~/.nanobot/config.json[/cyan]")
    console.print("     Get one at: https://openrouter.ai/keys")
    console.print('  2. Chat: [cyan]nanobot agent -m "Hello!"[/cyan]')
    console.print(
        "\n[dim]Want Telegram/WhatsApp? See: https://github.com/HKUDS/nanobot#-chat-apps[/dim]"
    )


def _make_provider(config: Config):
    """Create the appropriate LLM provider from config."""
    from nanobot.providers.custom_provider import CustomProvider
    from nanobot.providers.litellm_provider import LiteLLMProvider
    from nanobot.providers.openai_codex_provider import OpenAICodexProvider

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    if provider_name is None:
        console.print(f"[red]Error: Could not determine provider for model '{model}'.[/red]")
        raise typer.Exit(1)
    provider_name = str(provider_name)
    p = config.get_provider(model)

    # OpenAI Codex (OAuth)
    if provider_name == "openai_codex" or model.startswith("openai-codex/"):
        return OpenAICodexProvider(default_model=model)

    # Custom: direct OpenAI-compatible endpoint, bypasses LiteLLM
    if provider_name == "custom":
        return CustomProvider(
            api_key=p.api_key if p else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",
            default_model=model,
        )

    from nanobot.providers.registry import find_by_name

    spec = find_by_name(provider_name)
    if not model.startswith("bedrock/") and not (p and p.api_key) and not (spec and spec.is_oauth):
        console.print("[red]Error: No API key configured.[/red]")
        console.print("Set one in ~/.nanobot/config.json under providers section")
        raise typer.Exit(1)

    return LiteLLMProvider(
        api_key=p.api_key if p else None,
        api_base=config.get_api_base(model),
        default_model=model,
        extra_headers=p.extra_headers if p else None,
        provider_name=provider_name,
    )


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """Start the nanobot gateway."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.manager import ChannelManager
    from nanobot.config.loader import get_data_dir, load_config
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronJob
    from nanobot.heartbeat.service import HeartbeatService
    from nanobot.session.manager import SessionManager

    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG)

    console.print(f"{__logo__} Starting nanobot gateway on port {port}...")

    config = load_config()
    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    provider = _make_provider(config)
    session_manager = SessionManager(config.workspace_path)

    # Create cron service first (callback set after agent creation)
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Create agent with cron service
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        context_tokens=config.agents.defaults.context_tokens,
        reserve_tokens_floor=config.agents.defaults.reserve_tokens_floor,
        reasoning_effort=config.agents.defaults.reasoning_effort,
        brave_api_key=config.tools.web.search.api_key or None,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        subconscious_config=config.tools.subconscious,
    )

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        from nanobot.agent.tools.cron import CronTool
        from nanobot.agent.tools.message import MessageTool

        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )

        # Prevent the agent from scheduling new cron jobs during execution
        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)
        try:
            response = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
            )
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)

        message_tool = agent.tools.get("message")
        if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        if job.payload.deliver and job.payload.to and response:
            from nanobot.bus.events import OutboundMessage

            await bus.publish_outbound(
                OutboundMessage(
                    channel=job.payload.channel or "cli", chat_id=job.payload.to, content=response
                )
            )
        return response

    cron.on_job = on_cron_job

    # Create channel manager
    channels = ChannelManager(config, bus, session_manager=session_manager, agent_loop=agent)

    def _parse_iso_datetime(raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def _resolve_main_session_key() -> str:
        """Resolve heartbeat delivery session (shared default preferred)."""
        default_session = (config.agents.defaults.session or "").strip()
        if default_session:
            return default_session

        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if not key or key in {"heartbeat"}:
                continue
            if key.startswith(("system:", "cron:")):
                continue
            return key

        return "cli:direct"

    def _pick_heartbeat_target_for_session(session_key: str) -> tuple[str, str]:
        """Pick the best routable channel/chat target for a session key."""
        enabled = set(channels.enabled_channels)
        if ":" in session_key:
            channel, chat_id = session_key.split(":", 1)
            if channel not in {"cli", "system"} and channel in enabled and chat_id:
                return channel, chat_id
        return _pick_heartbeat_target()

    def _append_heartbeat_to_main_session(session_key: str, response: str) -> None:
        """Persist heartbeat output into main session as assistant text."""
        session = session_manager.get_or_create(session_key)
        session.messages.append(
            {
                "role": "assistant",
                "content": response,
                "timestamp": datetime.now().isoformat(),
                "source": "heartbeat",
            }
        )
        session.updated_at = datetime.now()
        session_manager.save(session)

    def _last_user_message_at() -> datetime | None:
        """Get timestamp of the latest user-authored message across sessions."""
        latest: datetime | None = None
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if not key or key == "heartbeat" or key.startswith(("system:", "cron:")):
                continue

            session = session_manager.get_or_create(key)
            for entry in reversed(session.messages):
                if entry.get("role") != "user":
                    continue
                ts = _parse_iso_datetime(entry.get("timestamp"))
                if ts and (latest is None or ts > latest):
                    latest = ts
                break

        return latest

    def _is_inactive_for(seconds: int) -> bool:
        """Return true when there have been no user messages recently."""
        if seconds <= 0:
            return False
        last_user_at = _last_user_message_at()
        if last_user_at is None:
            return True
        return (datetime.now() - last_user_at).total_seconds() >= seconds

    def _resolve_telegram_owner_chat_id(session_key: str) -> str | None:
        """Resolve Telegram owner chat_id for optional heartbeat duplication."""
        telegram_channel = channels.get_channel("telegram")
        if telegram_channel is None:
            return None

        session_map = getattr(telegram_channel, "_session_chat_ids", None)
        if isinstance(session_map, dict):
            chat_id = session_map.get(session_key)
            if isinstance(chat_id, str) and chat_id:
                return chat_id

        allow_from = config.channels.telegram.allow_from
        if allow_from:
            owner = allow_from[0].strip()
            if owner and owner != "*":
                owner_id = owner.split("|", 1)[0].strip()
                if owner_id:
                    return owner_id
        return None

    def _pick_heartbeat_target() -> tuple[str, str]:
        """Pick a routable channel/chat target for heartbeat-triggered messages."""
        enabled = set(channels.enabled_channels)
        # Prefer the most recently updated non-internal session on an enabled channel.
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        # Fallback keeps prior behavior but remains explicit.
        return "cli", "direct"

    # Create heartbeat service
    async def on_heartbeat_execute(tasks: str) -> str:
        """Phase 2: execute heartbeat tasks through the full agent loop."""
        main_session = _resolve_main_session_key()
        channel, chat_id = _pick_heartbeat_target_for_session(main_session)

        async def _silent(*_args, **_kwargs):
            pass

        return await agent.process_direct(
            tasks,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
            model=hb_cfg.model,
        )

    async def on_heartbeat_notify(response: str) -> None:
        """Save heartbeat output into main session and deliver to channels."""
        from nanobot.bus.events import OutboundMessage

        main_session = _resolve_main_session_key()
        _append_heartbeat_to_main_session(main_session, response)

        channel, chat_id = _pick_heartbeat_target_for_session(main_session)
        await bus.publish_outbound(
            OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=response,
                session_key=main_session,
                metadata={"source": "heartbeat"},
            )
        )

        inactive_window = hb_cfg.duplicate_to_telegram_after_inactive_s
        telegram_chat_id = _resolve_telegram_owner_chat_id(main_session)
        if (
            telegram_chat_id
            and _is_inactive_for(inactive_window)
            and not (channel == "telegram" and chat_id == telegram_chat_id)
        ):
            await bus.publish_outbound(
                OutboundMessage(
                    channel="telegram",
                    chat_id=telegram_chat_id,
                    content=response,
                    metadata={"source": "heartbeat", "duplicate": "inactive"},
                )
            )

    hb_cfg = config.gateway.heartbeat
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        provider=provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
        decide_model=hb_cfg.decide_model,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

    async def run():
        try:
            await cron.start()
            await heartbeat.start()
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        finally:
            await agent.close_mcp()
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()

    asyncio.run(run())


# ============================================================================
# Gateway Client Helpers
# ============================================================================


def _test_gateway_connection(socket_path: Path) -> bool:
    """Test if a running gateway is reachable on the Unix socket."""
    import socket

    if not socket_path.exists():
        return False

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(socket_path))
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False
    finally:
        sock.close()


def _run_as_client_single(
    socket_path: Path,
    message: str,
    session: str | None,
    render_markdown: bool,
) -> None:
    """Send a single message to the gateway and print the response."""

    async def _run():
        reader, writer = await asyncio.open_unix_connection(str(socket_path))

        # Read welcome
        welcome_line = await reader.readline()
        welcome = json.loads(welcome_line.decode().strip())
        chat_id = welcome.get("chatId", "")

        # Send message
        payload: dict = {"type": "message", "content": message}
        if session:
            payload["session"] = session
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()

        # Read responses until we get a non-progress, non-mirrored response
        response_text = ""
        while True:
            line = await reader.readline()
            if not line:
                break
            data = json.loads(line.decode().strip())
            source = data.get("from")
            if data.get("type") == "inbound" and source:
                print(f"\n\033[36m[{source}]\033[0m {data.get('content', '')}\n", flush=True)
            elif data.get("type") == "progress":
                prefix = f"[{source}] " if source else ""
                print(f"  \033[2m{prefix}↳ {data.get('content', '')}\033[0m", flush=True)
            elif data.get("type") == "response":
                if source:
                    print(f"\n\033[35m← {source}\033[0m: {data.get('content', '')}\n", flush=True)
                else:
                    response_text = data.get("content", "")
                    break

        writer.close()
        await writer.wait_closed()

        _print_agent_response(response_text, render_markdown=render_markdown)

    asyncio.run(_run())


def _run_as_client_interactive(
    socket_path: Path,
    session: str | None,
    render_markdown: bool,
) -> None:
    """Run an interactive session connected to the gateway."""
    _init_prompt_session()
    console.print(
        f"{__logo__} Connected to gateway (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n"
    )

    def _exit_on_sigint(signum, frame):
        _restore_terminal()
        console.print("\nGoodbye!")
        os._exit(0)

    signal.signal(signal.SIGINT, _exit_on_sigint)

    async def _run():
        reader, writer = await asyncio.open_unix_connection(str(socket_path))

        # Read welcome
        welcome_line = await reader.readline()
        welcome = json.loads(welcome_line.decode().strip())
        chat_id = welcome.get("chatId", "")

        turn_done = asyncio.Event()
        turn_done.set()
        turn_response: list[str] = []

        def _tty_write(text: str) -> None:
            """Write directly to the terminal, bypassing prompt_toolkit's stdout proxy."""
            try:
                fd = sys.stdout.fileno()
                os.write(fd, text.encode())
            except Exception:
                sys.stdout.write(text)
                sys.stdout.flush()

        async def _read_responses():
            while True:
                try:
                    line = await reader.readline()
                    if not line:
                        break
                    data = json.loads(line.decode().strip())
                    msg_type = data.get("type", "")
                    content = data.get("content", "")
                    source = data.get("from")  # set on mirrored messages

                    if msg_type == "inbound" and source:
                        _tty_write(f"\n\033[36m[{source}]\033[0m {content}\n\n")
                    elif msg_type == "progress":
                        prefix = f"[{source}] " if source else ""
                        _tty_write(f"  \033[2m{prefix}↳ {content}\033[0m\n")
                    elif msg_type == "response":
                        if source:
                            _tty_write(f"\n\033[35m← {source}\033[0m\n")
                            _tty_write(f"{content}\n\n")
                        elif not turn_done.is_set():
                            turn_response.append(content)
                            turn_done.set()
                        elif content:
                            _print_agent_response(content, render_markdown=render_markdown)
                except (json.JSONDecodeError, asyncio.CancelledError):
                    break

        read_task = asyncio.create_task(_read_responses())

        try:
            while True:
                try:
                    _flush_pending_tty_input()
                    user_input = await _read_interactive_input_async()
                    command = user_input.strip()
                    if not command:
                        continue

                    if _is_exit_command(command):
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break

                    turn_done.clear()
                    turn_response.clear()

                    payload: dict = {"type": "message", "content": user_input}
                    if session:
                        payload["session"] = session
                    writer.write(json.dumps(payload).encode() + b"\n")
                    await writer.drain()

                    # Wait for response with a spinner
                    with console.status("[dim]nanobot is thinking...[/dim]", spinner="dots"):
                        await turn_done.wait()

                    if turn_response:
                        _print_agent_response(turn_response[0], render_markdown=render_markdown)

                except KeyboardInterrupt:
                    _restore_terminal()
                    console.print("\nGoodbye!")
                    break
                except EOFError:
                    _restore_terminal()
                    console.print("\nGoodbye!")
                    break
        finally:
            read_task.cancel()
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    asyncio.run(_run())


# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    markdown: bool = typer.Option(
        True, "--markdown/--no-markdown", help="Render assistant output as Markdown"
    ),
    logs: bool = typer.Option(
        False, "--logs/--no-logs", help="Show nanobot runtime logs during chat"
    ),
):
    """Interact with the agent directly."""
    from loguru import logger

    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import get_data_dir, load_config
    from nanobot.cron.service import CronService

    config = load_config()
    sync_workspace_templates(config.workspace_path)

    # --- Gateway client mode: connect to running gateway if available ---
    socket_path = Path(config.channels.cli_socket.socket_path).expanduser()
    if config.channels.cli_socket.enabled and socket_path.exists():
        # Determine session override for gateway mode
        # If user kept the default session_id, use the gateway's default_session (omit field)
        # If user explicitly passed -s, forward it
        explicit_session = session_id if session_id != "cli:direct" else None

        if _test_gateway_connection(socket_path):
            try:
                if message:
                    console.print(f"[dim]Connected to gateway via {socket_path}[/dim]")
                    _run_as_client_single(socket_path, message, explicit_session, markdown)
                else:
                    _run_as_client_interactive(socket_path, explicit_session, markdown)
                return
            except ConnectionRefusedError:
                console.print(
                    "[yellow]Gateway not responding, falling back to standalone mode[/yellow]"
                )
            except Exception as e:
                console.print(
                    f"[yellow]Gateway connection failed ({e}), falling back to standalone[/yellow]"
                )
        else:
            console.print(
                "[yellow]Gateway socket exists but not reachable, falling back to standalone[/yellow]"
            )

    # --- Standalone mode (existing behavior) ---

    bus = MessageBus()
    provider = _make_provider(config)

    # Create cron service for tool usage (no callback needed for CLI unless running)
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    if logs:
        logger.enable("nanobot")
    else:
        logger.disable("nanobot")

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        context_tokens=config.agents.defaults.context_tokens,
        reserve_tokens_floor=config.agents.defaults.reserve_tokens_floor,
        reasoning_effort=config.agents.defaults.reasoning_effort,
        brave_api_key=config.tools.web.search.api_key or None,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        subconscious_config=config.tools.subconscious,
    )

    # Show spinner when logs are off (no output to miss); skip when logs are on
    def _thinking_ctx():
        if logs:
            from contextlib import nullcontext

            return nullcontext()
        # Animated spinner is safe to use with prompt_toolkit input handling
        return console.status("[dim]nanobot is thinking...[/dim]", spinner="dots")

    async def _cli_progress(content: str, *, tool_hint: bool = False) -> None:
        ch = agent_loop.channels_config
        if ch and tool_hint and not ch.send_tool_hints:
            return
        if ch and not tool_hint and not ch.send_progress:
            return
        console.print(f"  [dim]↳ {content}[/dim]")

    if message:
        # Single message mode — direct call, but with channel support for outbound messages
        async def run_once():
            from nanobot.channels.manager import ChannelManager

            # Create channel manager to handle outbound messages (e.g., to Telegram)
            channels = ChannelManager(config, bus)

            # Start channels briefly to handle outbound messages
            channel_task = None
            if channels.enabled_channels:
                channel_task = asyncio.create_task(channels.start_all())

            try:
                with _thinking_ctx():
                    response = await agent_loop.process_direct(
                        message, session_id, on_progress=_cli_progress
                    )

                # Give channels time to send any outbound messages
                await asyncio.sleep(0.5)

                _print_agent_response(response, render_markdown=markdown)
            finally:
                if channel_task:
                    channel_task.cancel()
                    await asyncio.gather(channel_task, return_exceptions=True)
                    await channels.stop_all()
                await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # Interactive mode — route through bus like other channels
        from nanobot.bus.events import InboundMessage

        _init_prompt_session()
        console.print(
            f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n"
        )

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        def _exit_on_sigint(signum, frame):
            _restore_terminal()
            console.print("\nGoodbye!")
            os._exit(0)

        signal.signal(signal.SIGINT, _exit_on_sigint)

        async def run_interactive():
            bus_task = asyncio.create_task(agent_loop.run())
            turn_done = asyncio.Event()
            turn_done.set()
            turn_response: list[str] = []

            async def _consume_outbound():
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                        if msg.metadata.get("_progress"):
                            is_tool_hint = msg.metadata.get("_tool_hint", False)
                            ch = agent_loop.channels_config
                            if ch and is_tool_hint and not ch.send_tool_hints:
                                pass
                            elif ch and not is_tool_hint and not ch.send_progress:
                                pass
                            else:
                                console.print(f"  [dim]↳ {msg.content}[/dim]")
                        elif not turn_done.is_set():
                            if msg.content:
                                turn_response.append(msg.content)
                            turn_done.set()
                        elif msg.content:
                            console.print()
                            _print_agent_response(msg.content, render_markdown=markdown)
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                while True:
                    try:
                        _flush_pending_tty_input()
                        user_input = await _read_interactive_input_async()
                        command = user_input.strip()
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\nGoodbye!")
                            break

                        turn_done.clear()
                        turn_response.clear()

                        await bus.publish_inbound(
                            InboundMessage(
                                channel=cli_channel,
                                sender_id="user",
                                chat_id=cli_chat_id,
                                content=user_input,
                            )
                        )

                        with _thinking_ctx():
                            await turn_done.wait()

                        if turn_response:
                            _print_agent_response(turn_response[0], render_markdown=markdown)
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from nanobot.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")
    table.add_column("Configuration", style="yellow")

    # WhatsApp
    wa = config.channels.whatsapp
    table.add_row("WhatsApp", "✓" if wa.enabled else "✗", wa.bridge_url)

    dc = config.channels.discord
    table.add_row("Discord", "✓" if dc.enabled else "✗", dc.gateway_url)

    # Feishu
    fs = config.channels.feishu
    fs_config = f"app_id: {fs.app_id[:10]}..." if fs.app_id else "[dim]not configured[/dim]"
    table.add_row("Feishu", "✓" if fs.enabled else "✗", fs_config)

    # Mochat
    mc = config.channels.mochat
    mc_base = mc.base_url or "[dim]not configured[/dim]"
    table.add_row("Mochat", "✓" if mc.enabled else "✗", mc_base)

    # Telegram
    tg = config.channels.telegram
    tg_config = f"token: {tg.token[:10]}..." if tg.token else "[dim]not configured[/dim]"
    table.add_row("Telegram", "✓" if tg.enabled else "✗", tg_config)

    # Slack
    slack = config.channels.slack
    slack_config = "socket" if slack.app_token and slack.bot_token else "[dim]not configured[/dim]"
    table.add_row("Slack", "✓" if slack.enabled else "✗", slack_config)

    # DingTalk
    dt = config.channels.dingtalk
    dt_config = (
        f"client_id: {dt.client_id[:10]}..." if dt.client_id else "[dim]not configured[/dim]"
    )
    table.add_row("DingTalk", "✓" if dt.enabled else "✗", dt_config)

    # QQ
    qq = config.channels.qq
    qq_config = f"app_id: {qq.app_id[:10]}..." if qq.app_id else "[dim]not configured[/dim]"
    table.add_row("QQ", "✓" if qq.enabled else "✗", qq_config)

    # Email
    em = config.channels.email
    em_config = em.imap_host if em.imap_host else "[dim]not configured[/dim]"
    table.add_row("Email", "✓" if em.enabled else "✗", em_config)

    console.print(table)


def _get_bridge_dir() -> Path:
    """Get the bridge directory, setting it up if needed."""
    import shutil
    import subprocess

    # User's bridge location
    user_bridge = Path.home() / ".nanobot" / "bridge"

    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # Check for npm
    if not shutil.which("npm"):
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # nanobot/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall nanobot")
        raise typer.Exit(1)

    console.print(f"{__logo__} Setting up bridge...")

    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run(["npm", "install"], cwd=user_bridge, check=True, capture_output=True)

        console.print("  Building...")
        subprocess.run(["npm", "run", "build"], cwd=user_bridge, check=True, capture_output=True)

        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)

    return user_bridge


@channels_app.command("login")
def channels_login():
    """Link device via QR code."""
    import subprocess

    from nanobot.config.loader import load_config

    config = load_config()
    bridge_dir = _get_bridge_dir()

    console.print(f"{__logo__} Starting bridge...")
    console.print("Scan the QR code to connect.\n")

    env = {**os.environ}
    if config.channels.whatsapp.bridge_token:
        env["BRIDGE_TOKEN"] = config.channels.whatsapp.bridge_token

    try:
        subprocess.run(["npm", "start"], cwd=bridge_dir, check=True, env=env)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")
    except FileNotFoundError:
        console.print("[red]npm not found. Please install Node.js.[/red]")


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show nanobot status."""
    from nanobot.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} nanobot Status\n")

    console.print(
        f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}"
    )
    console.print(
        f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}"
    )

    if config_path.exists():
        from nanobot.providers.registry import PROVIDERS

        console.print(f"Model: {config.agents.defaults.model}")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(
                    f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}"
                )


def _safe_int(value, default: int = 0) -> int:
    """Convert mixed metadata values to int."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_session_key(
    session_manager, requested_session: str, all_session_keys: list[str]
) -> str | None:
    """Resolve a session key by exact key or suffix match."""
    if requested_session in all_session_keys:
        return requested_session

    suffix_matches = [
        key for key in all_session_keys if ":" in key and key.split(":", 1)[1] == requested_session
    ]
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    if len(suffix_matches) > 1:
        console.print(
            f"[red]Ambiguous session '{requested_session}'[/red]. Matches: {', '.join(suffix_matches)}"
        )
        raise typer.Exit(1)

    return None


def _fallback_token_count(messages: list[dict[str, Any]]) -> int:
    """Fallback token estimator for prompt messages."""
    chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    chars += len(str(block.get("text", "")))
                elif block.get("type") == "image_url":
                    chars += 256
                else:
                    chars += len(str(block))
        else:
            chars += len(str(content))
        chars += 24
    return max(1, chars // 4)


def _count_tokens(messages: list[dict[str, Any]], model: str) -> int:
    """Count prompt tokens for a model with safe fallback."""
    if not messages:
        return 0

    try:
        from litellm import token_counter

        return int(token_counter(model=model, messages=messages) or 0)
    except Exception:
        return _fallback_token_count(messages)


def _context_usage_breakdown(messages: list[dict[str, Any]], model: str) -> dict[str, int]:
    """Compute system/history/current/total token usage."""
    total = _count_tokens(messages, model)
    if not messages:
        return {"system": 0, "history": 0, "current": 0, "total": total}

    system = _count_tokens(messages[:1], model)
    without_current = _count_tokens(messages[:-1], model) if len(messages) > 1 else system
    history = max(0, without_current - system)
    current = max(0, total - without_current)
    return {"system": system, "history": history, "current": current, "total": total}


def _extract_text_content(content: Any) -> str:
    """Normalize persisted message content into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(p for p in parts if p)
    return str(content)


def _usage_completion_tokens(message: dict[str, Any]) -> int | None:
    """Read completion/output tokens from persisted assistant usage."""
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    completion = usage.get("completion_tokens")
    if isinstance(completion, int):
        return completion
    output = usage.get("output_tokens")
    if isinstance(output, int):
        return output
    return None


def _usage_prompt_tokens(message: dict[str, Any]) -> int | None:
    """Read prompt/input tokens from persisted assistant usage."""
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    if isinstance(prompt, int):
        return prompt
    input_tokens = usage.get("input_tokens")
    if isinstance(input_tokens, int):
        return input_tokens
    return None


def _recompute_context_usage_for_session(config: Config, session_obj) -> dict[str, Any] | None:
    """Recompute per-session token totals from persisted user/assistant turns."""
    from nanobot.agent.context import ContextBuilder

    model = session_obj.metadata.get("model") if isinstance(session_obj.metadata, dict) else None
    if not isinstance(model, str) or not model.strip():
        model = config.agents.defaults.model

    context_builder = ContextBuilder(config.workspace_path)
    system_prompt = context_builder.build_system_prompt()
    system_prompt_tokens = _count_tokens([{"role": "system", "content": system_prompt}], model)

    totals = {
        "user_messages": 0,
        "user_request_tokens": 0,
        "agent_response_tokens": 0,
        "conversation_tokens": 0,
        "system_prompt_tokens": system_prompt_tokens,
        "llm_prompt_tokens": 0,
        "llm_completion_tokens": 0,
        "llm_total_tokens": 0,
    }

    for message in session_obj.messages:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = _extract_text_content(message.get("content"))
        if role == "user":
            token_count = _count_tokens([{"role": role, "content": text}], model)
            totals["user_messages"] += 1
            totals["user_request_tokens"] += token_count
        else:
            prompt_tokens = _usage_prompt_tokens(message)
            completion_tokens = _usage_completion_tokens(message)
            if prompt_tokens is not None:
                totals["llm_prompt_tokens"] += prompt_tokens
            if completion_tokens is None:
                completion_tokens = _count_tokens([{"role": role, "content": text}], model)
            totals["agent_response_tokens"] += completion_tokens
            totals["llm_completion_tokens"] += completion_tokens

    totals["conversation_tokens"] = totals["user_request_tokens"] + totals["agent_response_tokens"]
    totals["llm_total_tokens"] = totals["llm_prompt_tokens"] + totals["llm_completion_tokens"]

    if totals["user_messages"] == 0 and totals["agent_response_tokens"] == 0:
        return None

    return {
        "totals": totals,
        "meta": {
            "model": model,
        },
    }


def _system_prompt_breakdown(config: Config) -> tuple[int, list[tuple[str, int]]]:
    """Return total system prompt tokens and per-component contributions."""
    from nanobot.agent.context import ContextBuilder

    builder = ContextBuilder(config.workspace_path)
    parts: list[tuple[str, str]] = []

    identity = builder._get_identity()
    if identity:
        parts.append(("Identity", identity))

    for filename in builder.BOOTSTRAP_FILES:
        file_path = builder.workspace / filename
        if not file_path.exists():
            parts.append((f"Bootstrap: {filename} (missing)", ""))
            continue
        content = file_path.read_text(encoding="utf-8")
        parts.append((f"Bootstrap: {filename}", f"## {filename}\n\n{content}"))

    # Memory is now auto-injected by the subconscious service at runtime

    always_skills = builder.skills.get_always_skills()
    if always_skills:
        always_content = builder.skills.load_skills_for_context(always_skills)
        if always_content:
            parts.append(("Active Skills", f"# Active Skills\n\n{always_content}"))

    skills_summary = builder.skills.build_skills_summary()
    if skills_summary:
        parts.append(
            (
                "Skills Summary",
                "# Skills\n\n"
                "The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.\n"
                'Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.\n\n'
                f"{skills_summary}",
            )
        )

    model = config.agents.defaults.model
    breakdown: list[tuple[str, int]] = []
    assembled = ""
    prev_tokens = 0
    separator_tokens = 0

    for index, (label, content) in enumerate(parts):
        if content == "":
            breakdown.append((label, 0))
            continue
        if index == 0:
            next_prompt = content
        else:
            next_prompt = assembled + "\n\n---\n\n" + content
            sep_only_tokens = _count_tokens(
                [{"role": "system", "content": assembled + "\n\n---\n\n"}], model
            )
            separator_tokens += max(0, sep_only_tokens - prev_tokens)

        total_tokens = _count_tokens([{"role": "system", "content": next_prompt}], model)
        contribution = max(0, total_tokens - prev_tokens)
        breakdown.append((label, contribution))
        assembled = next_prompt
        prev_tokens = total_tokens

    if separator_tokens > 0 and len(parts) > 1:
        breakdown.append(("Separators", separator_tokens))

    return prev_tokens, breakdown


def _skills_breakdown(config: Config) -> list[tuple[str, int, int, bool]]:
    """Return per-skill summary and full-content token usage."""
    from nanobot.agent.skills import SkillsLoader

    loader = SkillsLoader(config.workspace_path)
    skills = loader.list_skills(filter_unavailable=False)
    model = config.agents.defaults.model
    always = set(loader.get_always_skills())

    def _escape_xml(value: str) -> str:
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    out: list[tuple[str, int, int, bool]] = []
    for skill in skills:
        name = skill["name"]
        esc_name = _escape_xml(name)
        desc = _escape_xml(loader._get_skill_description(name))
        skill_meta = loader._get_skill_meta(name)
        available = loader._check_requirements(skill_meta)

        summary_lines = [
            f'  <skill available="{str(available).lower()}">',
            f"    <name>{esc_name}</name>",
            f"    <description>{desc}</description>",
            f"    <location>{skill['path']}</location>",
        ]
        if not available:
            missing = loader._get_missing_requirements(skill_meta)
            if missing:
                summary_lines.append(f"    <requires>{_escape_xml(missing)}</requires>")
        summary_lines.append("  </skill>")
        summary_entry = "\n".join(summary_lines)
        summary_tokens = _count_tokens([{"role": "system", "content": summary_entry}], model)

        full_tokens = 0
        raw = loader.load_skill(name)
        if raw:
            stripped = loader._strip_frontmatter(raw)
            full_block = f"### Skill: {name}\n\n{stripped}"
            full_tokens = _count_tokens([{"role": "system", "content": full_block}], model)

        out.append((name, summary_tokens, full_tokens, name in always))

    return out


@app.command()
def context(
    session: str | None = typer.Option(
        None,
        "--session",
        "-s",
        help="Show context usage for one session (exact key or suffix)",
    ),
):
    """Show context token usage breakdown by session."""
    from nanobot.config.loader import load_config
    from nanobot.session.manager import SessionManager

    config = load_config()
    session_manager = SessionManager(config.workspace_path)
    listed = session_manager.list_sessions()
    all_keys = [s.get("key", "") for s in listed if s.get("key")]

    if not all_keys:
        console.print("No sessions found.")
        return

    target_key = None
    if session:
        target_key = _resolve_session_key(session_manager, session, all_keys)
        if not target_key:
            console.print(f"No session found for '{session}'.")
            raise typer.Exit(1)

    keys = [target_key] if target_key else all_keys

    rows: list[tuple[str, dict[str, Any]]] = []
    for key in keys:
        s = session_manager.get_or_create(key)
        recomputed = _recompute_context_usage_for_session(config, s)
        if recomputed:
            s.metadata["context_usage"] = recomputed
            session_manager.save(s)

        usage = s.metadata.get("context_usage", {}) if isinstance(s.metadata, dict) else {}
        if not isinstance(usage, dict):
            continue
        totals = usage.get("totals", {})
        if not isinstance(totals, dict):
            continue
        if (
            _safe_int(totals.get("user_messages")) <= 0
            and _safe_int(totals.get("agent_response_tokens")) <= 0
        ):
            continue
        rows.append((key, totals))

    if not rows:
        if target_key:
            console.print(f"No context usage data recorded yet for '{target_key}'.")
        else:
            console.print("No context usage data recorded yet.")
        return

    table = Table(title="Context Usage By Session")
    table.add_column("Session", style="cyan")
    table.add_column("User Msgs", justify="right")
    table.add_column("User Tokens", justify="right")
    table.add_column("Agent Tokens", justify="right")
    table.add_column("Sum", justify="right")
    table.add_column("System Prompt", justify="right")
    table.add_column("LLM Total", justify="right")

    for key, totals in rows:
        user_messages = _safe_int(totals.get("user_messages"))
        user_tokens = _safe_int(totals.get("user_request_tokens"))
        agent_tokens = _safe_int(totals.get("agent_response_tokens"))
        conversation_tokens = _safe_int(totals.get("conversation_tokens"))
        system_prompt_tokens = _safe_int(totals.get("system_prompt_tokens"))
        llm_total_tokens = _safe_int(totals.get("llm_total_tokens"))

        table.add_row(
            key,
            str(user_messages),
            str(user_tokens),
            str(agent_tokens),
            str(conversation_tokens),
            str(system_prompt_tokens),
            str(llm_total_tokens),
        )

    console.print(table)

    system_total, breakdown = _system_prompt_breakdown(config)
    skills_breakdown = _skills_breakdown(config)
    if breakdown:
        detail = Table(title="System Prompt Breakdown")
        detail.add_column("Component", style="cyan")
        detail.add_column("Tokens", justify="right")
        for label, tokens in breakdown:
            detail.add_row(label, str(tokens))
        detail.add_row("Total", str(system_total))

        skills_table = Table(title="Skills Breakdown")
        skills_table.add_column("Skill", style="cyan")
        skills_table.add_column("Summary", justify="right")
        skills_table.add_column("Full", justify="right")
        skills_table.add_column("Active", justify="center")

        for name, summary_tokens, full_tokens, active in skills_breakdown:
            skills_table.add_row(
                name,
                str(summary_tokens),
                str(full_tokens),
                "yes" if active else "",
            )

        if skills_breakdown:
            skills_table.add_row(
                "Total",
                str(sum(item[1] for item in skills_breakdown)),
                str(sum(item[2] for item in skills_breakdown)),
                "",
            )

        console.print(detail)
        console.print(skills_table)


# ============================================================================
# OAuth Login
# ============================================================================

provider_app = typer.Typer(help="Manage providers")
app.add_typer(provider_app, name="provider")


_LOGIN_HANDLERS: dict[str, Callable[[], None]] = {}


def _register_login(name: str):
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn

    return decorator


@provider_app.command("login")
def provider_login(
    provider: str = typer.Argument(
        ..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"
    ),
):
    """Authenticate with an OAuth provider."""
    from nanobot.providers.registry import PROVIDERS

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    handler()


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive

        token = None
        try:
            token = get_token()
        except Exception:
            pass
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(
            f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]"
        )
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    import asyncio

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    async def _trigger():
        from litellm import acompletion

        await acompletion(
            model="github_copilot/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )

    try:
        asyncio.run(_trigger())
        console.print("[green]✓ Authenticated with GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
