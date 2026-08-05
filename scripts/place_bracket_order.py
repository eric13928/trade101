"""
Bracket order: entry + real stop-loss + real profit target, submitted as
actual broker-side orders (not just numbers I calculated) -- so the exit is
enforced by moomoo even if nobody is actively watching.

Not available through the packaged skill scripts (those only wire up plain
market/limit orders) -- calls the SDK's STOP order type directly, per
SKILL.md's own guidance for anything the packaged scripts don't cover.

IMPORTANT limitation, be upfront about it: these two exit orders are NOT a
true broker-side OCO (one-cancels-other) pair. moomoo doesn't expose OCO
linking through this API for plain stock orders. Both orders sit live at
once. In a cash account this is safe in practice -- if the stop fills first,
the target order will simply be rejected when it tries to sell shares you no
longer hold (and vice versa) -- but the "losing" order will sit as a rejected
order in your history, not silently vanish. True cleanup (cancelling
whichever one didn't fill) needs an active check afterward -- see
cancel_unfilled_leg().
"""
import argparse
import sys
import time

from moomoo import (
    OpenSecTradeContext, TrdMarket, SecurityFirm, TrdSide, OrderType,
    TrdEnv, RET_OK, ModifyOrderOp, Session,
)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

DEFAULT_ACC_ID = 2614920  # paper account


def _ctx():
    return OpenSecTradeContext(host="127.0.0.1", port=11111,
                               filter_trdmarket=TrdMarket.US, security_firm=SecurityFirm.NONE)


def wait_for_fill(ctx, order_id, acc_id, trd_env, timeout=30, poll_interval=1.0):
    """Poll until the order is filled (or times out) -- returns (dealt_qty, dealt_avg_price, status)."""
    elapsed = 0
    while elapsed < timeout:
        ret, data = ctx.order_list_query(order_id=order_id, acc_id=acc_id, trd_env=trd_env, refresh_cache=True)
        if ret == RET_OK and not data.empty:
            row = data.iloc[0]
            status = row["order_status"]
            if status == "FILLED_ALL":
                return float(row["dealt_qty"]), float(row["dealt_avg_price"]), status
            if status in ("FAILED", "DISABLED", "DELETED", "CANCELLED_ALL"):
                return float(row["dealt_qty"]), float(row["dealt_avg_price"]), status
        time.sleep(poll_interval)
        elapsed += poll_interval
    return None, None, "TIMEOUT"


def place_bracket_order(code, qty, stop_price, target_price, entry_price=None,
                        acc_id=DEFAULT_ACC_ID, trd_env="SIMULATE", confirmed=False):
    trd_env_enum = TrdEnv.SIMULATE if trd_env == "SIMULATE" else TrdEnv.REAL
    if trd_env_enum == TrdEnv.REAL and not confirmed:
        entry_desc = f"limit @ ${entry_price}" if entry_price else "market"
        return {
            "status": "preview_only",
            "message": f"LIVE order preview: BUY {qty} {code} @ {entry_desc}, "
                      f"then stop @ ${stop_price}, target @ ${target_price}. "
                      f"Re-run with confirmed=True to actually submit.",
        }

    ctx = _ctx()
    result = {"code": code, "qty": qty}

    # 1. Entry. During regular hours, a market order is fine (day-trading speed
    # matters more than a few cents of slippage). Outside regular hours, market
    # orders are rejected outright -- must use a limit order with
    # fill_outside_rth + session=ETH instead (entry_price required in that case).
    if entry_price:
        ret, data = ctx.place_order(
            price=entry_price, qty=qty, code=code, trd_side=TrdSide.BUY, order_type=OrderType.NORMAL,
            fill_outside_rth=True, session=Session.ETH,
            trd_env=trd_env_enum, acc_id=acc_id, remark="bracket-entry",
        )
    else:
        ret, data = ctx.place_order(
            price=0, qty=qty, code=code, trd_side=TrdSide.BUY, order_type=OrderType.MARKET,
            trd_env=trd_env_enum, acc_id=acc_id, remark="bracket-entry",
        )
    if ret != RET_OK:
        ctx.close()
        result["status"] = "entry_failed"
        result["error"] = str(data)
        return result
    entry_order_id = data.iloc[0]["order_id"]
    result["entry_order_id"] = entry_order_id

    # 2. Wait for the entry to actually fill before placing exits -- exits need the real filled qty
    dealt_qty, dealt_price, status = wait_for_fill(ctx, entry_order_id, acc_id, trd_env_enum)
    result["entry_status"] = status
    result["dealt_qty"] = dealt_qty
    result["dealt_price"] = dealt_price
    if status != "FILLED_ALL" or not dealt_qty:
        ctx.close()
        result["status"] = "entry_not_filled"
        return result

    # 3. Stop-loss (real STOP order, triggers a market sell if price falls to stop_price)
    ret, data = ctx.place_order(
        price=0, qty=dealt_qty, code=code, trd_side=TrdSide.SELL, order_type=OrderType.STOP,
        aux_price=stop_price, trd_env=trd_env_enum, acc_id=acc_id, remark="bracket-stop",
    )
    if ret == RET_OK:
        result["stop_order_id"] = data.iloc[0]["order_id"]
    else:
        result["stop_order_error"] = str(data)

    # 4. Profit target (limit sell at target_price)
    ret, data = ctx.place_order(
        price=target_price, qty=dealt_qty, code=code, trd_side=TrdSide.SELL, order_type=OrderType.NORMAL,
        trd_env=trd_env_enum, acc_id=acc_id, remark="bracket-target",
    )
    if ret == RET_OK:
        result["target_order_id"] = data.iloc[0]["order_id"]
    else:
        result["target_order_error"] = str(data)

    ctx.close()
    result["status"] = "bracket_placed"
    return result


def cancel_unfilled_leg(code, stop_order_id, target_order_id, acc_id=DEFAULT_ACC_ID, trd_env="SIMULATE"):
    """Call after either the stop or target fills, to cancel whichever leg
    didn't -- since these aren't a true broker-side OCO pair, this cleanup
    has to be done explicitly, not automatically."""
    trd_env_enum = TrdEnv.SIMULATE if trd_env == "SIMULATE" else TrdEnv.REAL
    ctx = _ctx()
    results = {}
    for label, order_id in [("stop", stop_order_id), ("target", target_order_id)]:
        ret, data = ctx.order_list_query(order_id=order_id, acc_id=acc_id, trd_env=trd_env_enum, refresh_cache=True)
        if ret == RET_OK and not data.empty:
            status = data.iloc[0]["order_status"]
            if status not in ("FILLED_ALL", "CANCELLED_ALL", "FAILED", "DELETED"):
                ret2, msg = ctx.modify_order(modify_order_op=ModifyOrderOp.CANCEL, order_id=order_id,
                                             qty=0, price=0, acc_id=acc_id, trd_env=trd_env_enum)
                results[label] = "cancelled" if ret2 == RET_OK else f"cancel_failed: {msg}"
            else:
                results[label] = f"already {status}"
    ctx.close()
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Place a bracket order (entry + stop + target)")
    parser.add_argument("code")
    parser.add_argument("qty", type=int)
    parser.add_argument("stop_price", type=float)
    parser.add_argument("target_price", type=float)
    parser.add_argument("--entry-price", type=float, default=None,
                        help="Use a limit entry (required outside regular market hours)")
    parser.add_argument("--acc-id", type=int, default=DEFAULT_ACC_ID)
    parser.add_argument("--trd-env", choices=["SIMULATE", "REAL"], default="SIMULATE")
    parser.add_argument("--confirmed", action="store_true")
    args = parser.parse_args()

    r = place_bracket_order(args.code, args.qty, args.stop_price, args.target_price,
                            entry_price=args.entry_price,
                            acc_id=args.acc_id, trd_env=args.trd_env, confirmed=args.confirmed)
    for k, v in r.items():
        print(f"{k}: {v}")
