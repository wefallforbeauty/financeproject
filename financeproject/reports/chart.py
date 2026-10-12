"""Net worth chart (PNG): TRY on top, real TRY, USD and grams of gold below.

The four units have different scales, so each gets its own panel and axis
(small multiples) instead of a misleading dual axis. Each panel holds one
series, so there is no legend box: the panel title names it and the latest
value sits at the title's right. Lines only (no area fill), because the value
axes do not start at zero. Colors and marks follow the dataviz guide's
reference palette: slot-1 blue, 2 px line, 8 px end dot with a surface ring,
hairline solid grid.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from financeproject.core.dates import MONTHS_SHORT, month_label
from financeproject.core.money import format_amount

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
SERIES = "#2a78d6"

PANELS = (
    # column, title, unit, decimals
    ("nominal_try", "Net worth (TRY)", "TRY", 0),
    ("real_try", "Real TRY", "TRY", 0),
    ("usd", "US dollars", "USD", 0),
    ("gold_g", "Grams of gold", "g", 1),
)


def _date_locator(start, end, max_ticks: int):
    """Ticks on the first of every 1, 2, 3, 4, 6 or 12 months: the smallest step
    that keeps at most ``max_ticks`` ticks."""
    import matplotlib.dates as mdates

    span = (end.year - start.year) * 12 + (end.month - start.month) + 1
    if span <= 2:
        return mdates.AutoDateLocator(minticks=2, maxticks=max_ticks)
    step = next((k for k in (1, 2, 3, 4, 6, 12) if span / k <= max_ticks), max(12, -(-span // max_ticks)))
    if step >= 12:
        return mdates.YearLocator(base=max(1, step // 12))
    return mdates.MonthLocator(bymonth=range(1, 13, step) if 12 % step == 0 else None, interval=1 if 12 % step == 0 else step)


def _style(ax, max_ticks: int, start, end) -> None:
    import matplotlib.dates as mdates
    from matplotlib.ticker import FuncFormatter

    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASELINE)
    ax.spines["bottom"].set_linewidth(1)
    ax.grid(axis="y", color=GRID, linewidth=1, linestyle="-")
    ax.set_axisbelow(True)
    ax.tick_params(colors=MUTED, labelsize=9, length=0, pad=6)
    ax.xaxis.set_major_locator(_date_locator(start, end, max_ticks))
    ax.xaxis.set_major_formatter(
        FuncFormatter(lambda x, _p: (lambda d: f"{MONTHS_SHORT[d.month - 1]} {d:%y}")(mdates.num2date(x)))
    )


def _plot(ax, frame: pd.DataFrame, column: str, title: str, unit: str, decimals: int, big: bool) -> None:
    from matplotlib.ticker import FuncFormatter

    data = frame[["date", column]].dropna()
    dates = pd.to_datetime(frame["date"])
    _style(ax, 8 if big else 4, dates.min(), dates.max())
    size = 13 if big else 11
    ax.set_title(title, loc="left", color=INK, fontsize=size, fontweight="bold", pad=10)
    if data.empty:
        ax.text(0.5, 0.5, "no data", transform=ax.transAxes, ha="center", va="center", color=MUTED)
        ax.set_xticks([])
        ax.set_yticks([])
        return
    x = pd.to_datetime(data["date"])
    y = data[column].astype(float)
    ax.plot(x, y, color=SERIES, linewidth=2, solid_joinstyle="round", solid_capstyle="round")
    ax.scatter([x.iloc[-1]], [y.iloc[-1]], s=64, color=SERIES, edgecolors=SURFACE, linewidths=2, zorder=3)
    ax.set_title(f"{format_amount(y.iloc[-1], decimals)} {unit}", loc="right", color=INK, fontsize=size, pad=10)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: format_amount(v, 1 if unit == "g" else 0)))
    ax.margins(x=0.02, y=0.08)


def save_networth_chart(frame: pd.DataFrame, path: Path, base_month=None, subtitle: str | None = None) -> Path:
    """Write the chart to ``path`` and return it. ``frame`` comes from ``networth_series``."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(12, 8), dpi=120, facecolor=SURFACE)
    grid = fig.add_gridspec(2, 3, height_ratios=(1.6, 1), hspace=0.42, wspace=0.3)
    axes = [fig.add_subplot(grid[0, :])] + [fig.add_subplot(grid[1, i]) for i in range(3)]
    for i, (ax, (column, title, unit, decimals)) in enumerate(zip(axes, PANELS)):
        _plot(ax, frame, column, title, unit, decimals, big=i == 0)
    if subtitle:
        fig.text(0.125, 0.96, subtitle, color=INK_2, fontsize=9)
    notes = ["USD: TCMB forex buying rate", "Gold: XAU/USD (Frankfurter) × USD/TRY"]
    if base_month is not None:
        notes.insert(0, f"Real TRY: in {month_label(base_month)} prices (Turkish CPI, 2025=100)")
    fig.text(0.125, 0.02, " · ".join(notes), color=MUTED, fontsize=8)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    return path
