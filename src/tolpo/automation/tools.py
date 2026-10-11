"""Dispatch registered MCP tools with their declared tiers and their own guards."""
import inspect

from tolpo import policy
from tolpo.registry import Forbidden


class MCPToolExecutor:
    def __init__(self, ctx):
        self.ctx = ctx
        self.tiers = {}
        # Earlier tools have only hints. Use the higher level when their exact tier is absent.
        for tool in ctx.server._tool_manager.list_tools():
            hints = tool.annotations
            self.tiers[tool.name] = 0 if hints and hints.read_only_hint else 4 if hints and hints.destructive_hint else 2
        self.tiers.update({"set_power": 1, "set_light": 1})

    def record(self, name, tier):
        self.tiers[name] = int(tier)

    def _tool(self, name):
        # Rule management cannot call itself while an engine tick holds its lock.
        if name.startswith("automation_"):
            raise Forbidden("recursive automation tool action is refused")
        tool = self.ctx.server._tool_manager.get_tool(name)
        if tool is None or name not in self.tiers or tool.context_kwarg or tool.resolved_params:
            raise Forbidden("tool is unavailable for local automation")
        return tool

    def plan(self, name, params):
        tool = self._tool(name)
        validated = tool.fn_metadata.validate_arguments(params)
        tier = self.tiers[name]
        cfg = self.ctx.registry.cfg
        allowed = tier <= cfg.tier or name in cfg.allow_methods
        if name in ("set_power", "set_light"):
            refs = validated["targets"]
            refs = [refs] if isinstance(refs, str) else refs
            entries = [entry for ref in refs for entry in self.ctx.registry.resolve(ref)]
            allowed = allowed and bool(entries) and all(entry.kind in policy.POWER_KINDS for entry in entries)
        # A security/lifecycle tool must accept a fresh confirmation and check it in its caller.
        if tier >= 3 and "confirm" not in inspect.signature(tool.fn).parameters:
            allowed = False
        return {"level": tier, "allowed": allowed, "needs_confirmation": tier >= 3}

    async def execute(self, name, params, *, confirm):
        plan = self.plan(name, params)
        if not plan["allowed"] or (plan["needs_confirmation"] and not confirm):
            raise Forbidden("tool action is blocked by the owner's policy or confirmation")
        tool = self._tool(name)
        arguments = dict(params)
        if "confirm" in inspect.signature(tool.fn).parameters:
            arguments["confirm"] = confirm
        validated = tool.fn_metadata.validate_arguments(arguments)
        result = tool.fn(**validated)
        if inspect.isawaitable(result):
            result = await result
        # The tool retains its normal caller/registry checks. Device results are never echoed here.
        if isinstance(result, dict) and any(not row.get("ok", True) for row in result.get("results", [])):
            raise RuntimeError("tool reported a failed device action")
