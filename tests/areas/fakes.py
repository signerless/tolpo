"""A caller for area tests. The policy (declared methods, levels, confirmation) runs for real; only the wire is canned."""

from tolpo.calls import Caller


class FakeCaller(Caller):
    """Answers from `responses` and records what was sent.

    caller.respond("get_schedule_rules", {"rules": []})                    # for every device
    caller.respond("get_schedule_rules", {"rules": []}, slug="desk-lamp")  # for one device
    caller.respond("add_countdown_rule", lambda entry, params: {"id": "c1"})  # computed
    caller.respond("add_countdown_rule", DeviceUnavailable("..."))          # raises (an Exception instance)

    A method with no canned answer returns an empty dict. `caller.sent` lists (slug, method, params) in order.
    """

    def __init__(self, registry, responses=None):
        super().__init__(registry)
        self.responses = {}
        self.sent = []
        for method, value in (responses or {}).items():
            self.respond(method, value)

    def respond(self, method, result, slug=None):
        self.responses[(slug, method)] = result

    def methods_sent(self):
        return [method for _, method, _ in self.sent]

    async def _send(self, entry, request):
        out = {}
        for method, params in request.items():
            self.sent.append((entry.slug, method, dict(params or {})))
            value = self.responses.get((entry.slug, method), self.responses.get((None, method), {}))
            if isinstance(value, Exception):
                raise value
            out[method] = value(entry, params) if callable(value) else value
        return out
