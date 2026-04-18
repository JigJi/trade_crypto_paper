"""Leaderboard - rank models by performance metrics."""
from research.model_registry import ModelRegistry


def build_leaderboard(registry: ModelRegistry = None) -> list[dict]:
    """Build ranked leaderboard from model registry."""
    if registry is None:
        registry = ModelRegistry()

    models = registry.get_all()
    entries = []
    for m in models:
        if m.get("oos_pnl") is None:
            continue
        entries.append({
            "rank": 0,
            "version": m["version"],
            "status": m["status"],
            "is_champion": m.get("is_champion", False),
            "oos_pnl": m.get("oos_pnl", 0),
            "oos_sharpe": m.get("oos_sharpe"),
            "oos_win_rate": m.get("oos_win_rate"),
            "oos_trades": m.get("oos_trades"),
            "realistic_return_pct": m.get("realistic_return_pct"),
            "n_factors": len(m.get("factors", [])),
            "created": m.get("created"),
        })
    entries.sort(key=lambda x: x["oos_pnl"], reverse=True)
    for i, e in enumerate(entries):
        e["rank"] = i + 1
    return entries
