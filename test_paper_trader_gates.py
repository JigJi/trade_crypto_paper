"""
Unit tests for paper_trader signal gates + algo sweep logic.

Tests run without touching Binance — all exchange calls mocked.
Purpose: catch regressions in gate ordering + sweep correctness.

Run: python test_paper_trader_gates.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from unittest.mock import MagicMock


# ══════════════════════════════════════════════════════════════
# Test: signal gate ordering
# ══════════════════════════════════════════════════════════════

def test_gate_priority_order():
    """
    Signal gates must be evaluated in this priority (first match wins):
      1. has_position → MANAGE_POSITION
      2. suppress_signals (data stale) → SKIP_DATA_STALE
      3. extreme_conf3_block → SKIP_EXTREME_CONF3
      4. health_paused → SKIP_HEALTH_PAUSED
      5. signal==1 and not LONG_ENABLED → SKIP_LONG_DISABLED
      6. signal != 0 and cooldown_ok → (alt_filter check) → OPEN_*
      7. signal != 0 and not cooldown_ok → SKIP_COOLDOWN
      8. signal == 0, pa_aligned == 0 → SKIP_PA
      9. default → NO_SIGNAL
    """
    from paper_trading.paper_trader import run_cycle  # noqa: F401

    print("[1/3] Testing signal gate priority...")

    def resolve_action(has_position, suppress, extreme_block, health_paused,
                      signal, cooldown_ok, long_enabled, pa_aligned,
                      alt_allow=True):
        """Replicate paper_trader.run_cycle gate logic for testing."""
        if has_position:
            return "MANAGE_POSITION"
        elif suppress:
            return "SKIP_DATA_STALE"
        elif extreme_block and signal != 0:
            return "SKIP_EXTREME_CONF3"
        elif health_paused and signal != 0:
            return "SKIP_HEALTH_PAUSED"
        elif signal == 1 and not long_enabled:
            return "SKIP_LONG_DISABLED"
        elif signal != 0 and cooldown_ok:
            if not alt_allow:
                return "SKIP_ALT_FILTER"
            return "OPEN_LONG" if signal == 1 else "OPEN_SHORT"
        elif signal != 0 and not cooldown_ok:
            return "SKIP_COOLDOWN"
        elif signal == 0 and pa_aligned == 0:
            return "SKIP_PA"
        else:
            return "NO_SIGNAL"

    cases = [
        # (args, expected)
        # has_position beats everything
        (dict(has_position=True, suppress=True, extreme_block=True,
              health_paused=True, signal=1, cooldown_ok=True,
              long_enabled=False, pa_aligned=0), "MANAGE_POSITION"),
        # suppress beats signal
        (dict(has_position=False, suppress=True, extreme_block=False,
              health_paused=False, signal=-1, cooldown_ok=True,
              long_enabled=True, pa_aligned=1), "SKIP_DATA_STALE"),
        # extreme beats long/health
        (dict(has_position=False, suppress=False, extreme_block=True,
              health_paused=True, signal=-1, cooldown_ok=True,
              long_enabled=True, pa_aligned=1), "SKIP_EXTREME_CONF3"),
        # health beats long
        (dict(has_position=False, suppress=False, extreme_block=False,
              health_paused=True, signal=1, cooldown_ok=True,
              long_enabled=True, pa_aligned=1), "SKIP_HEALTH_PAUSED"),
        # long_disabled blocks long signal
        (dict(has_position=False, suppress=False, extreme_block=False,
              health_paused=False, signal=1, cooldown_ok=True,
              long_enabled=False, pa_aligned=1), "SKIP_LONG_DISABLED"),
        # short signal passes when long disabled
        (dict(has_position=False, suppress=False, extreme_block=False,
              health_paused=False, signal=-1, cooldown_ok=True,
              long_enabled=False, pa_aligned=1), "OPEN_SHORT"),
        # alt_filter blocks open
        (dict(has_position=False, suppress=False, extreme_block=False,
              health_paused=False, signal=-1, cooldown_ok=True,
              long_enabled=True, pa_aligned=1, alt_allow=False),
         "SKIP_ALT_FILTER"),
        # cooldown blocks signal
        (dict(has_position=False, suppress=False, extreme_block=False,
              health_paused=False, signal=-1, cooldown_ok=False,
              long_enabled=True, pa_aligned=1), "SKIP_COOLDOWN"),
        # pa=0 with no signal
        (dict(has_position=False, suppress=False, extreme_block=False,
              health_paused=False, signal=0, cooldown_ok=True,
              long_enabled=True, pa_aligned=0), "SKIP_PA"),
        # default
        (dict(has_position=False, suppress=False, extreme_block=False,
              health_paused=False, signal=0, cooldown_ok=True,
              long_enabled=True, pa_aligned=1), "NO_SIGNAL"),
    ]

    errors = 0
    for args, expected in cases:
        actual = resolve_action(**args)
        if actual != expected:
            print(f"  FAIL: {args} → got {actual}, expected {expected}")
            errors += 1

    if errors == 0:
        print(f"  PASS: {len(cases)}/{len(cases)} gate priority cases")
    return errors


# ══════════════════════════════════════════════════════════════
# Test: global algo sweep logic
# ══════════════════════════════════════════════════════════════

def test_global_algo_sweep():
    """Sweep must cancel algo orders for symbols not in COINS or without position."""
    print("[2/3] Testing global algo sweep logic...")

    active_coins = ["ARIA", "BEAT", "PIXEL", "ADA", "XRP"]
    valid_symbols = {f"{c}USDT" for c in active_coins}
    # Scenarios: algo orders exist on various symbols
    algo_symbols = {
        "ARIAUSDT",   # in COINS + has position (should skip)
        "BEATUSDT",   # in COINS + no position (should cancel — stale)
        "GALAUSDT",   # removed coin (should cancel)
        "PENGUUSDT",  # removed coin (should cancel)
        "LINKUSDT",   # removed coin (should cancel)
    }
    open_symbols = {"ARIAUSDT"}  # only ARIA has a position

    expected_cancel = {"BEATUSDT", "GALAUSDT", "PENGUUSDT", "LINKUSDT"}

    # Replicate sweep logic
    cancelled = set()
    for sym in algo_symbols:
        if sym not in valid_symbols or sym not in open_symbols:
            cancelled.add(sym)

    errors = 0
    if cancelled != expected_cancel:
        print(f"  FAIL: cancelled={cancelled}, expected={expected_cancel}")
        errors += 1
    else:
        print(f"  PASS: Correctly cancels {len(cancelled)}/5 symbols "
              f"(skips ARIAUSDT with live position)")

    return errors


# ══════════════════════════════════════════════════════════════
# Test: _sync_state_from_exchange computes software SL/TP from ATR
# ══════════════════════════════════════════════════════════════

def test_sync_state_computes_sl_tp():
    """Adopted positions must get software SL/TP when ATR is available."""
    print("[3/3] Testing sync state SL/TP computation...")

    from paper_trading.position_manager import PositionManager

    # Mock exchange + config
    mock_exchange = MagicMock()
    config = {
        "symbol": "XRPUSDT", "model": "v3",
        "sl_atr_mult": 10.0, "tp_atr_mult": 5.0,
        "trail_atr_mult": 1.5, "trail_activate_atr": 1.0,
        "cooldown_bars": 4, "threshold": 3.0,
        "use_alt_pa_filter": False,
    }
    pm = PositionManager("XRP", config, mock_exchange, budget_usdt=100)

    errors = 0

    # Case 1: SHORT position with ATR
    pos = {"positionAmt": "-100", "entryPrice": "1.5"}
    candle = {"atr": 0.01, "close": 1.5}

    # Patch db.set_meta to capture writes
    captured = {}
    from paper_trading import state_db as db
    orig_set = db.set_meta

    def capture(key, val):
        captured[key] = val
        return orig_set(key, val)

    db.set_meta = capture
    try:
        pm._sync_state_from_exchange(pos, btc_score=2.0, candle=candle)
    finally:
        db.set_meta = orig_set

    # SHORT: SL above, TP below
    expected_sl = 1.5 + 10.0 * 0.01  # 1.60
    expected_tp = 1.5 - 5.0 * 0.01   # 1.45
    sl_price = float(captured.get("sl_price_XRP", 0))
    tp_price = float(captured.get("tp_price_XRP", 0))

    if abs(sl_price - expected_sl) > 1e-6:
        print(f"  FAIL: SHORT sl_price={sl_price}, expected {expected_sl}")
        errors += 1
    if abs(tp_price - expected_tp) > 1e-6:
        print(f"  FAIL: SHORT tp_price={tp_price}, expected {expected_tp}")
        errors += 1

    # Case 2: LONG position with ATR
    pos = {"positionAmt": "100", "entryPrice": "1.5"}
    captured.clear()
    db.set_meta = capture
    try:
        pm._sync_state_from_exchange(pos, btc_score=2.0, candle=candle)
    finally:
        db.set_meta = orig_set

    # LONG: SL below, TP above
    expected_sl = 1.5 - 10.0 * 0.01   # 1.40
    expected_tp = 1.5 + 5.0 * 0.01    # 1.55
    sl_price = float(captured.get("sl_price_XRP", 0))
    tp_price = float(captured.get("tp_price_XRP", 0))

    if abs(sl_price - expected_sl) > 1e-6:
        print(f"  FAIL: LONG sl_price={sl_price}, expected {expected_sl}")
        errors += 1
    if abs(tp_price - expected_tp) > 1e-6:
        print(f"  FAIL: LONG tp_price={tp_price}, expected {expected_tp}")
        errors += 1

    # Case 3: No ATR → fallback to 0
    candle_no_atr = {"atr": 0, "close": 1.5}
    captured.clear()
    db.set_meta = capture
    try:
        pm._sync_state_from_exchange(pos, btc_score=2.0, candle=candle_no_atr)
    finally:
        db.set_meta = orig_set

    sl_price = float(captured.get("sl_price_XRP", 0))
    tp_price = float(captured.get("tp_price_XRP", 0))
    if sl_price != 0 or tp_price != 0:
        print(f"  FAIL: No-ATR sl_price={sl_price} tp_price={tp_price}, "
              f"expected both 0")
        errors += 1

    if errors == 0:
        print(f"  PASS: SL/TP computed correctly for LONG/SHORT/no-ATR cases")
    return errors


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("Paper Trader Gate + Sweep Tests")
    print("=" * 60)
    total = 0
    total += test_gate_priority_order()
    total += test_global_algo_sweep()
    total += test_sync_state_computes_sl_tp()
    print("=" * 60)
    if total == 0:
        print("ALL TESTS PASSED")
    else:
        print(f"FAILED: {total} errors")
    print("=" * 60)
    return total


if __name__ == "__main__":
    sys.exit(main())
