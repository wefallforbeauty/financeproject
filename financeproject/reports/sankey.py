"""Sankey diagram (PNG) of where a period's income went.

Three columns: the income sources on the left flow into the period's budget in
the middle, which flows out to the expense categories and to savings on the
right. When expenses exceed income, the gap comes from savings on the left.
Band widths are proportional to the amounts (the form goes back to Sankey's
1898 diagram of a steam engine's energy flows; Schmidt 2008).

Colours follow the role of each flow, not the category: income in slot-1 blue,
expenses in slot-2 orange, savings in slot-3 aqua (the dataviz reference
palette; the three slots pass every colour-vision check together). Every node
has a direct label with its amount and share of income, so no legend is
needed; the same numbers are in ``finance income-statement``. Each node sits in
a slot tall enough for its label, so labels never drift away from their node.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from financeproject.core.money import format_amount
from financeproject.reports.chart import BASELINE, INK, INK_2, MUTED, SURFACE

INCOME_COLOR = "#2a78d6"
EXPENSE_COLOR = "#eb6834"
SAVINGS_COLOR = "#1baf7a"
BUDGET_COLOR = BASELINE


@dataclass(frozen=True)
class Node:
    label: str
    value: Decimal
    color: str


def fold_small(values: dict[str, Decimal], top: int, other: str) -> list[tuple[str, Decimal]]:
    """The ``top - 1`` largest items and the rest summed as ``other`` (or all, if they fit)."""
    items = sorted(((k, v) for k, v in values.items() if v > 0), key=lambda kv: (-kv[1], kv[0]))
    if len(items) <= top:
        return items
    head, tail = items[: top - 1], items[top - 1 :]
    return head + [(other, sum((v for _, v in tail), Decimal(0)))]


def short(path: str) -> str:
    """``Expenses:Food`` → ``Food``; the root is implied by the column."""
    parts = path.split(":")
    return ":".join(parts[1:]) if len(parts) > 1 else path


@dataclass(frozen=True)
class SankeyData:
    left: list[Node]
    right: list[Node]
    total_income: Decimal
    total_expenses: Decimal

    @property
    def saved(self) -> Decimal:
        return self.total_income - self.total_expenses

    @property
    def budget(self) -> Decimal:
        return max(self.total_income, self.total_expenses)

    @property
    def grouped(self) -> bool:
        """True if small categories were folded into "Other"."""
        return any(n.label == "Other" for n in self.left + self.right)


def build(
    income: dict[str, Decimal], expenses: dict[str, Decimal], top_income: int = 5, top_expenses: int = 9
) -> SankeyData:
    """Nodes from income and expense totals (positive amounts, keyed by account path)."""
    total_in = sum((v for v in income.values() if v > 0), Decimal(0))
    total_out = sum((v for v in expenses.values() if v > 0), Decimal(0))
    left = [Node(short(k), v, INCOME_COLOR) for k, v in fold_small(income, top_income, "Income:Other")]
    right = [Node(short(k), v, EXPENSE_COLOR) for k, v in fold_small(expenses, top_expenses, "Expenses:Other")]
    if total_in > total_out:
        right.append(Node("Saved", total_in - total_out, SAVINGS_COLOR))
    elif total_out > total_in:
        left.append(Node("From savings", total_out - total_in, SAVINGS_COLOR))
    return SankeyData(left, right, total_in, total_out)


def _column_height(nodes: list[Node], scale: float, gap: float, slot: float) -> float:
    return sum(max(float(n.value) * scale, slot) for n in nodes) + gap * max(len(nodes) - 1, 0)


def _scale(columns: list[list[Node]], budget: float, height: float, gap: float, slot: float) -> float:
    """The largest scale (height per unit of money) at which every column fits."""
    lo, hi = 0.0, height / budget
    if all(_column_height(c, hi, gap, slot) <= height for c in columns):
        return hi
    for _ in range(60):
        mid = (lo + hi) / 2
        if all(_column_height(c, mid, gap, slot) <= height for c in columns):
            lo = mid
        else:
            hi = mid
    return lo


def _layout(nodes: list[Node], scale: float, top: float, gap: float, slot: float) -> list[tuple[float, float]]:
    """(top, bottom) of each node, stacked downwards from ``top``, each centred in a
    slot at least ``slot`` high."""
    out, y = [], top
    for node in nodes:
        h = float(node.value) * scale
        room = max(h, slot)
        pad = (room - h) / 2
        out.append((y - pad, y - pad - h))
        y -= room + gap
    return out


def _band(ax, x0: float, top0: float, bottom0: float, x1: float, top1: float, bottom1: float, color: str) -> None:
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MplPath

    xm = (x0 + x1) / 2
    vertices = [
        (x0, top0), (xm, top0), (xm, top1), (x1, top1),
        (x1, bottom1), (xm, bottom1), (xm, bottom0), (x0, bottom0),
        (x0, top0),
    ]
    codes = [
        MplPath.MOVETO, MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4,
        MplPath.LINETO, MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4,
        MplPath.CLOSEPOLY,
    ]
    ax.add_patch(PathPatch(MplPath(vertices, codes), facecolor=color, alpha=0.32, edgecolor=SURFACE, linewidth=0.8))


def save_sankey(data: SankeyData, path: Path, title: str, unit: str = "TRY", footnote: str | None = None) -> Path:
    """Draw the diagram to ``path`` (PNG) and return the path."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    fig = plt.figure(figsize=(12, 7.5), dpi=120, facecolor=SURFACE)
    ax = fig.add_axes((0.0, 0.0, 1.0, 1.0))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    fig.text(0.04, 0.955, title, color=INK, fontsize=15, fontweight="bold", va="top")
    if data.total_income > 0 and data.saved >= 0:
        outcome = f"saved {format_amount(data.saved, 0)} {unit} ({float(data.saved / data.total_income) * 100:.0f} % of income)"
    elif data.saved >= 0:
        outcome = "nothing spent"
    else:
        outcome = f"spent {format_amount(-data.saved, 0)} {unit} more than came in"
    summary = (
        f"Income {format_amount(data.total_income, 0)} {unit} · "
        f"expenses {format_amount(data.total_expenses, 0)} {unit} · {outcome}"
    )
    fig.text(0.04, 0.905, summary, color=INK_2, fontsize=10, va="top")
    if footnote:
        fig.text(0.04, 0.025, footnote, color=MUTED, fontsize=8, va="bottom")

    if data.budget <= 0:
        ax.text(0.5, 0.5, "no income or expenses in this period", ha="center", va="center", color=MUTED)
        return _save(fig, path)

    top, bottom, gap, width, slot = 0.84, 0.09, 0.008, 0.014, 0.052
    x_left, x_mid, x_right = 0.27, 0.495, 0.72
    scale = _scale([data.left, data.right], float(data.budget), top - bottom, gap, slot)
    left = _layout(data.left, scale, top, gap, slot)
    right = _layout(data.right, scale, top, gap, slot)
    mid_top = top
    mid_bottom = top - float(data.budget) * scale

    # Bands: left nodes → budget and budget → right nodes, stacked in node order.
    y = mid_top
    for node, (t, b) in zip(data.left, left):
        _band(ax, x_left + width, t, b, x_mid, y, y - (t - b), node.color)
        y -= t - b
    y = mid_top
    for node, (t, b) in zip(data.right, right):
        _band(ax, x_mid + width, y, y - (t - b), x_right, t, b, node.color)
        y -= t - b

    for nodes, boxes, x in ((data.left, left, x_left), (data.right, right, x_right)):
        for node, (t, b) in zip(nodes, boxes):
            ax.add_patch(Rectangle((x, b), width, max(t - b, 0.002), facecolor=node.color, edgecolor="none"))
    ax.add_patch(Rectangle((x_mid, mid_bottom), width, mid_top - mid_bottom, facecolor=BUDGET_COLOR, edgecolor="none"))

    def share(value: Decimal) -> str:
        if data.total_income <= 0:
            return ""
        pct = float(value / data.total_income) * 100
        return " · <1 %" if pct < 0.5 else f" · {pct:.0f} %"

    for nodes, boxes, x, align in (
        (data.left, left, x_left - 0.012, "right"),
        (data.right, right, x_right + width + 0.012, "left"),
    ):
        for node, (t, b) in zip(nodes, boxes):
            yc = (t + b) / 2
            ax.text(x, yc + 0.011, node.label, ha=align, va="center", color=INK, fontsize=10)
            ax.text(x, yc - 0.013, f"{format_amount(node.value, 0)} {unit}{share(node.value)}", ha=align, va="center", color=INK_2, fontsize=9)
    ax.text(x_mid + width / 2, mid_top + 0.022, "Budget", ha="center", va="bottom", color=INK, fontsize=10, fontweight="bold")
    return _save(fig, path)


def _save(fig, path: Path) -> Path:
    import matplotlib.pyplot as plt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path
