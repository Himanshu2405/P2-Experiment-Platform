"""Shared look of the app: one stylesheet, coloured chips and the tints used for good, bad and neutral numbers. Colours are
translucent so they read on both the light and the dark theme."""
import html

import streamlit as st

GOOD, BAD, NEUTRAL = "rgba(34, 197, 94, 0.24)", "rgba(239, 68, 68, 0.24)", "rgba(234, 179, 8, 0.28)"
GREY = "rgba(148, 163, 184, 0.26)"       # a clear answer of "no difference"; yellow is kept for flat lift and "inconclusive"
TONES = {  # chip tone -> (background, text, border)
    "green": ("rgba(34,197,94,0.16)", "#16a34a", "rgba(34,197,94,0.45)"),
    "red": ("rgba(239,68,68,0.16)", "#dc2626", "rgba(239,68,68,0.45)"),
    "yellow": ("rgba(234,179,8,0.2)", "#b7791f", "rgba(234,179,8,0.55)"),
    "blue": ("rgba(59,130,246,0.16)", "#2563eb", "rgba(59,130,246,0.45)"),
    "grey": ("rgba(148,163,184,0.18)", "#64748b", "rgba(148,163,184,0.5)"),
    "teal": ("rgba(20,184,166,0.16)", "#0d9488", "rgba(20,184,166,0.45)"),
}
STATUS_TONE = {"Draft": "grey", "Designed": "blue", "Running": "yellow", "Analyzed": "green", "Decided": "teal"}
VERDICT_TONE = {"Significant improvement": "green", "Passed": "green", "Significant decline": "red", "Failed": "red",
                "No significant difference": "grey", "Not significant": "grey", "Inconclusive": "yellow"}

CSS = """
<style>
/* spacing: the page starts right under the top bar */
[data-testid="stMainBlockContainer"] { padding-top: 3.6rem; padding-bottom: 2.5rem; max-width: 100%; }
h1 { font-size: 1.85rem !important; font-weight: 700 !important; padding: 0 0 .2rem 0 !important; margin: 0 0 .35rem 0 !important; }
h2, h3 { font-size: 1.15rem !important; font-weight: 650 !important; padding: .1rem 0 .15rem 0 !important; margin: .35rem 0 .2rem 0 !important; }
[data-testid="stVerticalBlock"] { gap: .7rem; }
/* cards */
[data-testid="stVerticalBlockBorderWrapper"] { border-radius: .75rem; box-shadow: 0 1px 2px rgba(15, 23, 42, .08); }
[data-testid="stMetric"] { border: 1px solid rgba(148,163,184,.35); border-radius: .75rem; padding: .7rem .9rem;
                           background: rgba(59,130,246,.06); min-height: 8.4rem; }
[data-testid="stMetricLabel"] p { font-size: .8rem; opacity: .8; text-transform: uppercase; letter-spacing: .03em; }
[data-testid="stMetricValue"] { font-size: 1.6rem; font-weight: 700; }
.verdict-card { border: 1px solid rgba(148,163,184,.35); border-radius: .75rem; padding: .7rem .9rem; background: rgba(59,130,246,.06); min-height: 8.4rem; box-sizing: border-box; }
.verdict-card .label { font-size: .8rem; opacity: .8; text-transform: uppercase; letter-spacing: .03em; margin-bottom: .45rem; }
/* tabs, buttons, tables, progress */
[data-testid="stTab"] { font-weight: 600; padding: .55rem 1.1rem; margin-right: .5rem; }
[data-testid="stTab"][aria-selected="true"] { font-weight: 750; }
[data-testid="stTabs"] [role="tablist"] { gap: .3rem; }
/* a thin progress bar drawn by hand so each row can have its own colour */
.pbar { height: .5rem; border-radius: 999px; background: rgba(148,163,184,.3); overflow: hidden; margin-top: .25rem; }
.pbar > div { height: 100%; border-radius: 999px; }
.pbar-text { font-size: .85rem; }
[data-testid="stDataFrame"] { border-radius: .6rem; overflow: hidden; border: 1px solid rgba(148,163,184,.4); }
[data-testid="stExpander"] { border-radius: .6rem; }
[data-testid="stAlert"] { border-radius: .6rem; }
/* chips */
.chip { display: inline-block; padding: .08rem .6rem; border-radius: 999px; font-size: .76rem; font-weight: 600; border: 1px solid; line-height: 1.35; word-break: keep-all; overflow-wrap: normal; }
.meta { font-size: .85rem; opacity: .85; margin: .15rem 0; }
.meta b { opacity: 1; }
.section-note { font-size: .8rem; opacity: .75; margin-top: -.2rem; }
/* catalog rows */
.st-key-cat_header { background: rgba(59,130,246,.10); border: 1px solid rgba(148,163,184,.4); border-radius: .6rem; padding: .35rem .6rem; }
[class*="st-key-cat_row_"] { border: 1px solid rgba(148,163,184,.35); border-radius: .6rem; padding: .35rem .6rem; background: rgba(148,163,184,.05); }
[class*="st-key-cat_row_"]:hover { background: rgba(59,130,246,.07); border-color: rgba(59,130,246,.45); }
/* the running indicator */
[data-testid="stStatusWidget"] { background: #3b82f6; color: #fff; padding: 4px 12px; border-radius: 6px; font-weight: 600; }
[data-testid="stStatusWidget"] * { color: #fff !important; }
</style>
"""


def progress_bar(fraction: float, text: str, tone: str) -> str:
    """A thin coloured bar with a sentence above it (blue while live, green when ended, yellow before the start)."""
    colour = {"blue": "#3b82f6", "green": "#22c55e", "yellow": "#eab308", "grey": "#94a3b8"}[tone]
    return (f'<div class="pbar-text">{html.escape(text)}</div>'
            f'<div class="pbar"><div style="width:{max(0.0, min(1.0, fraction)) * 100:.0f}%;background:{colour}"></div></div>')


def short_name(name: str) -> str:
    """'Priya (Checkout owner)' becomes 'Priya'."""
    return name.split(" (")[0]


def inject() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def chip(text: str, tone: str = "grey") -> str:
    bg, fg, border = TONES[tone]
    return f'<span class="chip" style="background:{bg};color:{fg};border-color:{border}">{html.escape(text)}</span>'


def status_chip(status: str) -> str:
    return chip(status, STATUS_TONE.get(status, "grey"))


def verdict_chip(verdict: str | None, fallback: str = "") -> str:
    if not verdict:
        return chip(fallback, "grey") if fallback else ""
    return chip(verdict, VERDICT_TONE.get(verdict, "grey"))
