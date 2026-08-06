"""Chart rendering for reports — matplotlib (Agg backend) → PNG bytes.

CivMC relevance: leadership needs to see population growth / decline trends
over time. The bot already stores ``monthly_snapshots`` but only ever showed
them as text. This module renders them as visual charts attached to Discord
embeds.

All charts are rendered to ``io.BytesIO`` (no disk writes) using the Agg
backend, which is non-interactive and safe for server use.

Functions are synchronous (matplotlib is not async). Callers should run them
in an executor to avoid blocking the event loop:

    png_bytes = await asyncio.get_event_loop().run_in_executor(
        None, render_population_trends, snapshots
    )
"""

import io
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Set the Agg backend BEFORE importing pyplot — this must happen at module
# load time so matplotlib never tries to open a display window.
import matplotlib  # noqa: E402 — must set Agg before importing pyplot

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.dates import DateFormatter  # noqa: E402

# CivMC-ish palette (green = active/growth, red = inactive/decline, blue =
# neutral totals). Avoids indigo/blue per project styling rules.
COLOR_TOTAL = "#5865F2"  # discord blurple — for total population
COLOR_ACTIVE = "#3BAD4C"  # CivMC green — for active citizens
COLOR_INACTIVE = "#ED4245"  # discord red — for inactive
COLOR_GRID = "#3a3a3a"
COLOR_TEXT = "#dcddde"
COLOR_BG = "#36393f"  # discord dark-mode background


def _apply_dark_style():
    """Apply a Discord-dark-friendly style so charts look good in embeds."""
    plt.rcParams.update(
        {
            "figure.facecolor": COLOR_BG,
            "axes.facecolor": COLOR_BG,
            "axes.edgecolor": COLOR_GRID,
            "axes.labelcolor": COLOR_TEXT,
            "xtick.color": COLOR_TEXT,
            "ytick.color": COLOR_TEXT,
            "text.color": COLOR_TEXT,
            "axes.grid": True,
            "grid.color": COLOR_GRID,
            "grid.alpha": 0.3,
            "font.size": 10,
            "axes.titlesize": 13,
            "axes.titleweight": "bold",
        }
    )


def _close_all(figs):
    """Close all figures to avoid memory leaks (matplotlib holds refs)."""
    for f in figs:
        plt.close(f)


def render_population_trends(
    snapshots: list[dict[str, Any]],
    top_settlements: list[dict[str, Any]] | None = None,
) -> bytes | None:
    """Render a 2-panel population-trends chart from monthly snapshots.

    Args:
        snapshots: rows from ``monthly_snapshots`` where ``district IS NULL``
            (i.e. duchy/province-level totals). Must contain snapshot_date,
            duchy, total, active.
        top_settlements: optional rows for the top N settlements (district IS
            NOT NULL) to render as a third panel. Each row: snapshot_date,
            district, total.

    Returns:
        PNG bytes, or None if there's not enough data to chart.
    """
    if not snapshots:
        return None

    _apply_dark_style()

    # --- Panel 1+2: National total + active vs inactive ---
    # Aggregate all duchies per snapshot_date into national totals.
    by_date: dict[Any, dict[str, int]] = {}
    for s in snapshots:
        d = s["snapshot_date"]
        if d not in by_date:
            by_date[d] = {"total": 0, "active": 0}
        by_date[d]["total"] += s["total"] or 0
        by_date[d]["active"] += s["active"] or 0

    dates = sorted(by_date.keys())
    totals = [by_date[d]["total"] for d in dates]
    actives = [by_date[d]["active"] for d in dates]
    inactives = [t - a for t, a in zip(totals, actives, strict=True)]

    has_settlements = bool(top_settlements)
    if has_settlements:
        fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(10, 12))
    else:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8))

    # Panel 1: Total population over time
    ax1.plot(dates, totals, color=COLOR_TOTAL, marker="o", linewidth=2.5, markersize=6)
    ax1.fill_between(dates, totals, alpha=0.15, color=COLOR_TOTAL)
    ax1.set_title("National Population Over Time")
    ax1.set_ylabel("Registered Citizens")
    ax1.xaxis.set_major_formatter(DateFormatter("%b %Y"))
    if len(dates) > 1:
        ax1.set_xlim(dates[0], dates[-1])

    # Panel 2: Active vs Inactive (stacked area)
    ax2.stackplot(
        dates,
        actives,
        inactives,
        labels=["Active", "Inactive"],
        colors=[COLOR_ACTIVE, COLOR_INACTIVE],
        alpha=0.8,
    )
    ax2.plot(dates, actives, color=COLOR_ACTIVE, linewidth=2, marker="o", markersize=5)
    ax2.set_title("Active vs Inactive Population")
    ax2.set_ylabel("Citizens")
    ax2.set_xlabel("Snapshot Date")
    ax2.xaxis.set_major_formatter(DateFormatter("%b %Y"))
    ax2.legend(loc="upper left", framealpha=0.8)
    if len(dates) > 1:
        ax2.set_xlim(dates[0], dates[-1])

    # Panel 3 (optional): Top settlements growth
    if has_settlements:
        assert top_settlements is not None  # has_settlements implies non-None
        # Group by district, collect (date, total) series.
        settlement_series: dict[str, list[tuple]] = {}
        for s in top_settlements:
            d = s["district"]
            settlement_series.setdefault(d, []).append((s["snapshot_date"], s["total"]))

        # Sort each series by date and plot.
        colors_cycle = [
            "#3BAD4C",
            "#FAA61A",
            "#E91E63",
            "#9B59B6",
            "#1ABC9C",
            "#E67E22",
            "#3498DB",
            "#F1C40F",
        ]
        for idx, (district, series) in enumerate(
            sorted(
                settlement_series.items(), key=lambda x: x[1][-1][1] if x[1] else 0, reverse=True
            )
        ):
            series.sort(key=lambda t: t[0])
            sd = [t[0] for t in series]
            sv = [t[1] for t in series]
            ax3.plot(
                sd,
                sv,
                marker="o",
                linewidth=2,
                markersize=5,
                label=district[:20],
                color=colors_cycle[idx % len(colors_cycle)],
            )

        ax3.set_title("Top Settlements — Population Growth")
        ax3.set_ylabel("Citizens")
        ax3.set_xlabel("Snapshot Date")
        ax3.xaxis.set_major_formatter(DateFormatter("%b %Y"))
        ax3.legend(loc="upper left", framealpha=0.8, fontsize=8)
        if sd and len(sd) > 1:
            ax3.set_xlim(sd[0], sd[-1])

    fig.tight_layout(pad=2.0)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, facecolor=COLOR_BG)
    _close_all([fig])
    buf.seek(0)
    return buf.getvalue()


def render_activity_series(
    title: str,
    dates: list,
    totals: list[int],
    actives: list[int] | None = None,
) -> bytes | None:
    """Render a single-panel activity time-series chart (Phase 3.5).

    Args:
        title: Chart title (e.g. "Florraine — Population & Activity").
        dates: X-axis values (snapshot_date or datetime).
        totals: Total population at each point.
        actives: Optional active citizen count at each point. When provided,
            a second line + shaded area is drawn showing active vs inactive.

    Returns:
        PNG bytes, or None if there's not enough data (fewer than 1 point).
    """
    if not dates or not totals:
        return None

    _apply_dark_style()
    fig, ax1 = plt.subplots(1, 1, figsize=(10, 6))

    # Total population line.
    ax1.plot(
        dates, totals, color=COLOR_TOTAL, marker="o", linewidth=2.5, markersize=6, label="Total"
    )
    ax1.fill_between(dates, totals, alpha=0.15, color=COLOR_TOTAL)
    ax1.set_title(title)
    ax1.set_ylabel("Citizens")
    ax1.xaxis.set_major_formatter(DateFormatter("%b %Y"))
    if len(dates) > 1:
        ax1.set_xlim(dates[0], dates[-1])

    # Active vs inactive stacked area (optional second series).
    if actives and len(actives) == len(totals):
        inactives = [t - a for t, a in zip(totals, actives, strict=True)]
        ax1.stackplot(
            dates,
            actives,
            inactives,
            labels=["Active", "Inactive"],
            colors=[COLOR_ACTIVE, COLOR_INACTIVE],
            alpha=0.3,
        )
        ax1.plot(
            dates,
            actives,
            color=COLOR_ACTIVE,
            linewidth=2,
            marker="s",
            markersize=4,
            label="Active",
        )
        ax1.legend(loc="upper left", framealpha=0.8)

    fig.tight_layout(pad=2.0)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, facecolor=COLOR_BG)
    _close_all([fig])
    buf.seek(0)
    return buf.getvalue()


def render_server_trends(
    title: str,
    data: list[tuple],
    period: str = "day",
) -> bytes | None:
    """Render a single-panel player-count time-series chart (WS-5).

    Args:
        title: Chart title (e.g. "CivMC Player Count — Last 24h").
        data: List of (timestamp, player_count) tuples sorted ascending.
            timestamp can be a datetime or epoch float.
        period: One of "day", "hour", "minute" — controls the x-axis
            date formatter and the stats summary (peak/low/avg).

    Returns:
        PNG bytes, or None if there's not enough data (fewer than 1 point).
    """
    if not data:
        return None

    _apply_dark_style()
    fig, ax = plt.subplots(1, 1, figsize=(10, 5))

    # Split the parallel arrays.
    timestamps = [d[0] for d in data]
    counts = [d[1] for d in data]

    # Player count line — filled area underneath for visibility.
    ax.plot(
        timestamps,
        counts,
        color=COLOR_ACTIVE,
        marker="",
        linewidth=2,
        markersize=0,
        label="Players online",
    )
    ax.fill_between(timestamps, counts, alpha=0.25, color=COLOR_ACTIVE)

    ax.set_title(title)
    ax.set_ylabel("Players Online")

    # X-axis formatter depends on the period.
    if period == "minute":
        ax.xaxis.set_major_formatter(DateFormatter("%H:%M"))
        xlabel = "Time (UTC, last 60 min)"
    elif period == "hour":
        ax.xaxis.set_major_formatter(DateFormatter("%H:%M"))
        xlabel = "Time (UTC, last 24h)"
    else:  # day
        ax.xaxis.set_major_formatter(DateFormatter("%m/%d %H:%M"))
        xlabel = "Time (UTC, last 24h)"

    ax.set_xlabel(xlabel)

    # Annotate peak + low points so leadership can see when the server is busy.
    if len(counts) > 1:
        peak_idx = counts.index(max(counts))
        low_idx = counts.index(min(counts))
        peak_ts, peak_val = timestamps[peak_idx], counts[peak_idx]
        low_ts, low_val = timestamps[low_idx], counts[low_idx]

        ax.annotate(
            f"Peak: {peak_val}",
            xy=(peak_ts, peak_val),
            xytext=(10, 10),
            textcoords="offset points",
            fontsize=9,
            color=COLOR_ACTIVE,
            fontweight="bold",
            arrowprops={"arrowstyle": "->", "color": COLOR_ACTIVE, "lw": 1.5},
        )
        ax.annotate(
            f"Low: {low_val}",
            xy=(low_ts, low_val),
            xytext=(10, -20),
            textcoords="offset points",
            fontsize=9,
            color=COLOR_INACTIVE,
            fontweight="bold",
            arrowprops={"arrowstyle": "->", "color": COLOR_INACTIVE, "lw": 1.5},
        )

    if len(timestamps) > 1:
        ax.set_xlim(timestamps[0], timestamps[-1])

    # Stats summary as a text box (top-right, below the title).
    if counts:
        avg = sum(counts) / len(counts)
        stats_text = f"Peak: {max(counts)}  •  Low: {min(counts)}  •  Avg: {avg:.1f}"
        fig.text(
            0.99,
            0.01,
            stats_text,
            ha="right",
            va="bottom",
            fontsize=9,
            color=COLOR_TEXT,
            bbox={
                "facecolor": COLOR_GRID,
                "edgecolor": COLOR_GRID,
                "alpha": 0.6,
                "boxstyle": "round,pad=0.4",
            },
        )

    fig.tight_layout(pad=2.0)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, facecolor=COLOR_BG)
    _close_all([fig])
    buf.seek(0)
    return buf.getvalue()
