"""Other branches around the one on screen, as small link boxes ("stubs").

A branch's graph shows its own steps. Every branch of the same project that shares
steps with it (or is its parent or fork) appears as one stub attached to the last
shared step, saying where it differs. A crowd at one step (a sweep of 20) folds
into a few stubs and "+N more", which opens them in the Experiments table.
"""

from __future__ import annotations

from urllib.parse import quote

from .collect import BranchInfo, NodeView
from .lineage import SHORT, STUB

MAX_STUBS = 4


def _shared(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


def _parent_of(info: BranchInfo) -> str | None:
    if info.forked_from:
        return info.forked_from.split("@", 1)[0]
    return info.from_branch


def _related(a: BranchInfo, b: BranchInfo, shared: int) -> bool:
    return shared > 0 or _parent_of(a) == b.name or _parent_of(b) == a.name


def difference(a: tuple[str, ...], b: tuple[str, ...], n: int, nodes: dict[str, NodeView]) -> str:
    """Where branch b leaves branch a, after n shared steps."""
    if n == len(a) == len(b):
        return "same steps"
    if n == len(b):
        return f"stops after step {n}"
    if n == len(a):
        return f"then {SHORT.get(nodes[b[n]].brick, nodes[b[n]].brick)}" if b[n] in nodes else "continues"
    mine, theirs = nodes.get(a[n]), nodes.get(b[n])
    if mine is None or theirs is None:
        return f"differs at step {n + 1}"
    if mine.brick != theirs.brick:
        return f"step {n + 1}: {SHORT.get(theirs.brick, theirs.brick)}"
    changed = [k for k in sorted(mine.params.keys() | theirs.params.keys()) if mine.params.get(k) != theirs.params.get(k)]
    return ", ".join(f"{k}={theirs.params.get(k)}" for k in changed[:2]) or f"differs at step {n + 1}"


def _stub(key: str, parent: str, dataset: str, name: str, subtitle: str, state: str, href: str, label: str) -> NodeView:
    return NodeView(key=key, parent=parent, dataset=dataset, brick=STUB, state=state, headline=subtitle,
                    params={"name": name, "href": href}, labels=(label,))


def _crowd(group: list[tuple[BranchInfo, str]], attach: str, dataset: str, project: str) -> NodeView:
    sweeps = {info.sweep for info, _ in group}
    target = f"sweep={quote(sweeps.pop())}" if len(sweeps) == 1 and None not in sweeps else f"project={quote(project)}"
    return _stub(f"stub:+{attach}", attach, dataset, f"+{len(group)} more", "open in Experiments", "PLANNED",
                 f"#experiments/{target}", f"{project}/+{len(group)}")


def relative_stubs(info: BranchInfo, peers: list[BranchInfo], nodes: dict[str, NodeView],
                   views: dict[str, str]) -> list[NodeView]:
    """Stubs for the branches around `info` (peers: its project's branches), attached where
    they part from it."""
    if not info.keys or info.keys[0] not in nodes:
        return []
    root = nodes[info.keys[0]].parent
    dataset = nodes[info.keys[0]].dataset
    by_attach: dict[str, list[tuple[BranchInfo, str]]] = {}
    for other in peers:
        if other.project != info.project or other.name == info.name or not other.keys:
            continue
        n = _shared(info.keys, other.keys)
        if not _related(info, other, n):
            continue
        attach = info.keys[n - 1] if n else root
        by_attach.setdefault(attach, []).append((other, difference(info.keys, other.keys, n, nodes)))
    stubs = []
    for attach, group in by_attach.items():
        group.sort(key=lambda item: (not item[0].label.pinned, item[0].name))
        shown = group if len(group) <= MAX_STUBS else group[: MAX_STUBS - 1]
        for other, subtitle in shown:
            label = f"{other.project}/{other.name}"
            stubs.append(_stub(f"stub:{label}", attach, dataset, other.name, subtitle, other.state,
                               f"#pipelines/{views[label]}" if label in views else "#experiments", label))
        if len(group) > len(shown):
            stubs.append(_crowd(group[len(shown):], attach, dataset, info.project))
    return stubs
