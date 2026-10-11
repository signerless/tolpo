"""Rule chains. A rule may follow other rules, but never in a cycle."""
from __future__ import annotations


def check_rule_graph(rules) -> None:
    """Refuse a cycle among `rule` triggers. Linear in rules plus edges, and never recursive, so a long chain of
    rules cannot block the event loop or hit the recursion limit."""
    graph = {r.id: [t.rule for t in r.triggers if t.kind == "rule"] for r in rules}
    done, active = set(), set()
    for root in graph:
        if root in done:
            continue
        active.add(root)
        stack = [(root, iter(graph[root]))]
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                stack.pop()
                active.discard(node)
                done.add(node)
            elif child in active:
                raise ValueError("automation rule cycle is refused")
            elif child in graph and child not in done:
                active.add(child)
                stack.append((child, iter(graph[child])))
