---
type: "explain"
date: "2026-08-05T02:58:12.441953+00:00"
question: "Why does get_bars connect nearly every strategy and risk-management community in this codebase - is it a clean shared utility, or a hidden coupling point?"
contributor: "graphify"
source_nodes: ["get_bars"]
---

# Q: Why does get_bars connect nearly every strategy and risk-management community in this codebase - is it a clean shared utility, or a hidden coupling point?

## Answer

get_bars (engine/utils/bars.py:209) has degree 65 with 64 incoming edges and only 1 outgoing (to _get_bars_alpaca) - a pure fan-in sink. Dependents span equity strategies (16 from equity/strategies.py), options pricing, market regime detection, position sizing (get_dynamic_tier), scan guardrails, kill_mode.check (the bear-market circuit breaker), and backtest/prediction scripts. Verdict: clean architecturally (single sanctioned data-access boundary, avoids each strategy hitting the broker API directly) but a real concentration-of-risk operationally - all 64 dependents share the same failure mode, evidenced by the session's own logs showing repeated 'Alpaca data stale (Ns > 120s) - skipping' warnings that simultaneously degrade signal generation, regime detection, sizing, AND the kill-mode safety check since they all read through the same function.

## Source Nodes

- get_bars