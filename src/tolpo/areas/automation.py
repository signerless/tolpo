"""MCP tools for local Smart Actions. No cloud calls."""
import asyncio
import copy
import logging
from contextlib import suppress
from typing import Annotated, Any

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BeforeValidator, Field, ValidationError

from tolpo import policy
from tolpo.automation import Engine, Rule, Settings
from tolpo.automation.engine import public_rule
from tolpo.automation.tools import MCPToolExecutor
from tolpo.policy import Tier

log = logging.getLogger(__name__)

METHODS = [
    # UNVERIFIED: direct hub and switch support needs model-specific hardware checks.
    # The method and sensor log shape are documented in python-kasa TriggerLogs.
    policy.method("get_trigger_logs", Tier.READ, ["sensor", "switch", "hub"], "Read the local sensor or button trigger log."),
]


def engine_for(ctx):
    """One engine per registry; lifecycle hooks live entirely in this area."""
    if not hasattr(ctx.registry, "_automation_engine"):
        ctx.registry._automation_engine = Engine(
            ctx.registry, ctx.caller, tool_executor=getattr(ctx.registry, "_automation_tools", None))
    return ctx.registry._automation_engine


def install_lifecycle(ctx):
    registry = ctx.registry
    if getattr(registry, "_automation_lifecycle", False):
        return
    registry._automation_lifecycle = True
    original_start, original_stop = registry.start, registry.stop

    async def start(*args, **kwargs):
        await original_start(*args, **kwargs)
        try:
            engine = engine_for(ctx)
        except Exception as exc:
            log.error("automation store could not be loaded: %s", type(exc).__name__)
            return
        engine.closed = False
        engine.start()

    async def stop():
        engine = getattr(registry, "_automation_engine", None)
        if engine is not None:
            await engine.stop()
        await original_stop()

    registry.start, registry.stop = start, stop


def rule_input(value):
    try:
        return Rule.model_validate(value)
    except Exception:
        raise ToolError("invalid automation rule") from None


def settings_input(value):
    try:
        return Settings.model_validate(value)
    except Exception:
        raise ToolError("invalid automation settings") from None


def tools(ctx):
    if ctx.server is not None:
        adapter = MCPToolExecutor(ctx)
        ctx.registry._automation_tools = adapter
        original_tool = ctx.tool

        def register(*, title, tier=Tier.READ, **options):
            decorate = original_tool(title=title, tier=tier, **options)
            def record(fn):
                adapter.record(options.get("name", fn.__name__), tier)
                return decorate(fn)
            return record

        ctx.tool = register
    install_lifecycle(ctx)

    def engine():
        try:
            return engine_for(ctx)
        except Exception as exc:
            raise ToolError(f"automation store could not be loaded: {type(exc).__name__}") from None

    async def change(fn):
        eng = engine()
        async with eng.lock:
            before = copy.deepcopy(eng.state)
            try:
                return fn(eng)
            except (ValueError, ValidationError):
                eng.state = before
                # Validation errors may echo private input values. Do not return them.
                raise ToolError("automation change refused: invalid rule, settings, id or cycle") from None
            except OSError:
                eng.state = before
                raise ToolError("automation state could not be written") from None

    @ctx.tool(title="Create local automation", tier=Tier.SETTING)
    async def automation_create(rule: Annotated[Rule, BeforeValidator(rule_input), Field(description="Local rule with unique id, triggers, conditions and ordered actions. Disabled by default.")]) -> dict[str, Any]:
        """Create a local rule. Actions use the owner's current policy at every execution."""
        return await change(lambda eng: eng.create(rule))

    @ctx.tool(title="List local automations")
    async def automation_list() -> dict[str, Any]:
        """List compact local rule summaries and the kill switch state."""
        eng = engine()
        async with eng.lock:
            return {"rules": [public_rule(r) for r in eng.state.rules], "killed": eng.state.settings.killed}

    @ctx.tool(title="Show local automation")
    async def automation_show(rule_id: Annotated[str, Field(description="Local rule id")]) -> dict[str, Any]:
        """Show a compact rule summary. Private action parameters are omitted."""
        return await change(lambda eng: public_rule(eng.rule(rule_id)))

    @ctx.tool(title="Enable local automation", tier=Tier.SETTING)
    async def automation_enable(rule_id: Annotated[str, Field(description="Local rule id")]) -> dict[str, Any]:
        """Enable a rule and lazily start local evaluation while the server runs."""
        return await change(lambda eng: eng.enabled(rule_id, True))

    @ctx.tool(title="Disable local automation", tier=Tier.SETTING)
    async def automation_disable(rule_id: Annotated[str, Field(description="Local rule id")]) -> dict[str, Any]:
        """Disable a rule and cancel its queued delayed actions."""
        return await change(lambda eng: eng.enabled(rule_id, False))

    @ctx.tool(title="Delete local automation", tier=Tier.SETTING)
    async def automation_delete(rule_id: Annotated[str, Field(description="Local rule id")]) -> dict[str, Any]:
        """Delete a local rule and its queued actions."""
        return await change(lambda eng: eng.delete(rule_id))

    @ctx.tool(title="Preview local automations")
    async def automation_dry_run() -> dict[str, Any]:
        """Read current observations and say what would fire now without actions or state changes."""
        return await engine().tick(dry_run=True)

    @ctx.tool(title="Evaluate local automations", tier=Tier.CONTROL)
    async def automation_tick(confirm: Annotated[str | None, Field(description="Optional per-call confirmation phrase for an immediate security or lifecycle action. Never saved.")] = None) -> dict[str, Any]:
        """Evaluate once, run due actions through policy, and report individual failures."""
        return await engine().tick(confirm=confirm)

    @ctx.tool(title="Read local automation alerts", tier=Tier.CONTROL)
    async def automation_alerts(clear: Annotated[bool, Field(description="Clear only the returned outbox records after reading them")] = False,
                                limit: Annotated[int, Field(ge=1, le=1000, description="Maximum records to return")] = 100) -> dict[str, Any]:
        """Read the owner's JSONL alert outbox. Quiet records should be delivered after quiet hours."""
        eng = engine()
        async with eng.lock:
            return {"alerts": eng.store.alerts(clear=clear, limit=limit)}

    @ctx.tool(title="Configure local automation", tier=Tier.SETTING)
    async def automation_settings(settings: Annotated[Settings, BeforeValidator(settings_input), Field(description="Complete local settings: IANA timezone, coordinates, poll interval, quiet hours and killed switch")]) -> dict[str, Any]:
        """Replace engine settings. Killing cancels pending runs and blocks ticks until cleared."""
        canceled = []
        def update(eng):
            eng.state.settings = settings
            if settings.killed:
                eng.state.runs = []
                eng.previous = {}
                eng.holds = {}
                eng.log_ids = {}
            eng.save()
            if settings.killed and eng.task:
                canceled.append(eng.task)
                eng.task.cancel()
                eng.task = None
            eng.start()
            return {"settings": settings.model_dump(exclude_none=True)}
        result = await change(update)
        for task in canceled:
            with suppress(asyncio.CancelledError):
                await task
        return result
