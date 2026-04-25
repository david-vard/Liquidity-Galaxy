
from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List, Optional, Tuple
import json
import math


class Trader:
    PRODUCTS = ["INTARIAN_PEPPER_ROOT", "ASH_COATED_OSMIUM"]
    LIMITS = {
        "INTARIAN_PEPPER_ROOT": 80,
        "ASH_COATED_OSMIUM": 80,
    }

    # Empirically, pepper drifts upward by about 0.001 per timestamp unit.
    PEPPER_TREND_SLOPE = 0.001

    def bid(self):
        # Balanced MAF bid from the research notebook.
        return 1501

    def run(self, state: TradingState):
        memory = self._load_memory(state.traderData)

        if state.timestamp == 0:
            memory = {
                "pepper_open_mid": None,
                "ema_mid": {},
            }

        orders: Dict[str, List[Order]] = {p: [] for p in self.PRODUCTS}

        for product in self.PRODUCTS:
            depth = state.order_depths.get(product)
            if depth is None:
                continue

            position = state.position.get(product, 0)
            best_bid, best_bid_vol, best_ask, best_ask_vol = self._best_prices(depth)
            mid = self._mid_from_book(best_bid, best_ask)
            if mid is None:
                continue

            imbalance = self._imbalance(best_bid_vol, best_ask_vol)
            ema_mid = self._update_ema(memory, product, mid)

            if product == "INTARIAN_PEPPER_ROOT":
                if memory.get("pepper_open_mid") is None:
                    memory["pepper_open_mid"] = mid

                fair = self._pepper_fair_value(
                    timestamp=state.timestamp,
                    mid=mid,
                    imbalance=imbalance,
                    position=position,
                    open_mid=memory["pepper_open_mid"],
                )
                product_orders = self._trade_pepper(
                    product=product,
                    depth=depth,
                    position=position,
                    fair=fair,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    timestamp=state.timestamp,
                )
            else:
                fair = self._osmium_fair_value(
                    ema_mid=ema_mid,
                    mid=mid,
                    imbalance=imbalance,
                    position=position,
                )
                product_orders = self._trade_osmium(
                    product=product,
                    depth=depth,
                    position=position,
                    fair=fair,
                    best_bid=best_bid,
                    best_ask=best_ask,
                )

            orders[product].extend(product_orders)

        trader_data = json.dumps(memory)
        return orders, 0, trader_data

    def _load_memory(self, trader_data: str) -> dict:
        if not trader_data:
            return {
                "pepper_open_mid": None,
                "ema_mid": {},
            }
        try:
            loaded = json.loads(trader_data)
            if not isinstance(loaded, dict):
                raise ValueError("Memory is not a dictionary")
            loaded.setdefault("pepper_open_mid", None)
            loaded.setdefault("ema_mid", {})
            return loaded
        except Exception:
            return {
                "pepper_open_mid": None,
                "ema_mid": {},
            }

    def _best_prices(
        self, depth: OrderDepth
    ) -> Tuple[Optional[int], int, Optional[int], int]:
        best_bid = max(depth.buy_orders.keys()) if depth.buy_orders else None
        best_ask = min(depth.sell_orders.keys()) if depth.sell_orders else None
        best_bid_vol = depth.buy_orders.get(best_bid, 0) if best_bid is not None else 0
        best_ask_vol = -depth.sell_orders.get(best_ask, 0) if best_ask is not None else 0
        return best_bid, best_bid_vol, best_ask, best_ask_vol

    def _mid_from_book(
        self, best_bid: Optional[int], best_ask: Optional[int]
    ) -> Optional[float]:
        if best_bid is not None and best_ask is not None:
            return 0.5 * (best_bid + best_ask)
        if best_bid is not None:
            return float(best_bid)
        if best_ask is not None:
            return float(best_ask)
        return None

    def _imbalance(self, bid_vol: int, ask_vol: int) -> float:
        total = bid_vol + ask_vol
        if total <= 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    def _update_ema(self, memory: dict, product: str, mid: float) -> float:
        alpha = 0.18
        prev = memory["ema_mid"].get(product)
        ema = mid if prev is None else alpha * mid + (1.0 - alpha) * prev
        memory["ema_mid"][product] = ema
        return ema

    def _pepper_target_position(self, timestamp: int) -> int:
        # Hold a large long in pepper for most of the session.
        if timestamp < 15000:
            return 70
        if timestamp < 85000:
            return 80
        return 70

    def _pepper_fair_value(
        self,
        timestamp: int,
        mid: float,
        imbalance: float,
        position: int,
        open_mid: float,
    ) -> float:
        trend_anchor = open_mid + self.PEPPER_TREND_SLOPE * timestamp
        target_pos = self._pepper_target_position(timestamp)

        # Stronger long bias than V1:
        #   - trend anchor
        #   - mild premium
        #   - positive adjustment when under target inventory
        base = 0.55 * trend_anchor + 0.45 * mid + 6.0
        signal = 7.5 * imbalance
        inventory_push = 0.18 * (target_pos - position)

        return base + signal + inventory_push

    def _osmium_fair_value(
        self,
        ema_mid: float,
        mid: float,
        imbalance: float,
        position: int,
    ) -> float:
        local_anchor = 0.75 * ema_mid + 0.25 * mid
        signal = 4.8 * imbalance
        inv_penalty = 0.22 * position
        return local_anchor + signal - inv_penalty

    def _trade_pepper(
        self,
        product: str,
        depth: OrderDepth,
        position: int,
        fair: float,
        best_bid: Optional[int],
        best_ask: Optional[int],
        timestamp: int,
    ) -> List[Order]:
        limit = self.LIMITS[product]
        orders: List[Order] = []
        pos = position
        target_pos = self._pepper_target_position(timestamp)

        # More aggressive accumulation than V1.
        buy_take_edge = 0.5
        sell_take_edge = 8.0

        for ask in sorted(depth.sell_orders.keys()):
            ask_qty = -depth.sell_orders[ask]
            if ask <= fair - buy_take_edge and pos < limit:
                qty = min(ask_qty, limit - pos)
                if qty > 0:
                    orders.append(Order(product, ask, qty))
                    pos += qty

        # Only sell when the bid is genuinely rich or we are very full.
        for bid in sorted(depth.buy_orders.keys(), reverse=True):
            bid_qty = depth.buy_orders[bid]
            rich_enough = bid >= fair + sell_take_edge
            reducing_overfill = pos > target_pos + 8 and bid >= fair + 4.0
            if (rich_enough or reducing_overfill) and pos > -limit:
                qty = min(bid_qty, pos + limit)
                if qty > 0:
                    orders.append(Order(product, bid, -qty))
                    pos -= qty

        # Passive quoting:
        # overwhelmingly prefer bid exposure, especially while below target.
        if pos < target_pos - 30:
            bid_size, ask_size = 20, 0
        elif pos < target_pos - 10:
            bid_size, ask_size = 14, 0
        elif pos < target_pos + 5:
            bid_size, ask_size = 10, 2
        elif pos < 78:
            bid_size, ask_size = 6, 4
        else:
            bid_size, ask_size = 2, 8

        bid_size = min(bid_size, limit - pos)
        ask_size = min(ask_size, pos + limit)

        # Bid more tightly than V1 so we get long earlier.
        if pos < target_pos - 10:
            bid_width = 2.0
        else:
            bid_width = 3.0
        ask_width = 14.0

        bid_px = self._passive_bid_price(
            fair=fair,
            width=bid_width,
            best_bid=best_bid,
            best_ask=best_ask,
        )
        ask_px = self._passive_ask_price(
            fair=fair,
            width=ask_width,
            best_bid=best_bid,
            best_ask=best_ask,
        )

        if bid_size > 0 and bid_px is not None:
            orders.append(Order(product, bid_px, bid_size))
        if ask_size > 0 and ask_px is not None:
            orders.append(Order(product, ask_px, -ask_size))

        return orders

    def _trade_osmium(
        self,
        product: str,
        depth: OrderDepth,
        position: int,
        fair: float,
        best_bid: Optional[int],
        best_ask: Optional[int],
    ) -> List[Order]:
        limit = self.LIMITS[product]
        orders: List[Order] = []
        pos = position

        take_edge = 2.0

        for ask in sorted(depth.sell_orders.keys()):
            ask_qty = -depth.sell_orders[ask]
            if ask <= fair - take_edge and pos < limit:
                qty = min(ask_qty, limit - pos)
                if qty > 0:
                    orders.append(Order(product, ask, qty))
                    pos += qty

        for bid in sorted(depth.buy_orders.keys(), reverse=True):
            bid_qty = depth.buy_orders[bid]
            if bid >= fair + take_edge and pos > -limit:
                qty = min(bid_qty, pos + limit)
                if qty > 0:
                    orders.append(Order(product, bid, -qty))
                    pos -= qty

        if pos <= -50:
            bid_size, ask_size = 12, 3
        elif pos < -15:
            bid_size, ask_size = 10, 6
        elif pos <= 15:
            bid_size, ask_size = 9, 9
        elif pos < 50:
            bid_size, ask_size = 6, 10
        else:
            bid_size, ask_size = 3, 12

        bid_size = min(bid_size, limit - pos)
        ask_size = min(ask_size, pos + limit)

        width = 8.0
        bid_px = self._passive_bid_price(
            fair=fair,
            width=width,
            best_bid=best_bid,
            best_ask=best_ask,
        )
        ask_px = self._passive_ask_price(
            fair=fair,
            width=width,
            best_bid=best_bid,
            best_ask=best_ask,
        )

        if bid_size > 0 and bid_px is not None:
            orders.append(Order(product, bid_px, bid_size))
        if ask_size > 0 and ask_px is not None:
            orders.append(Order(product, ask_px, -ask_size))

        return orders

    def _passive_bid_price(
        self,
        fair: float,
        width: float,
        best_bid: Optional[int],
        best_ask: Optional[int],
    ) -> Optional[int]:
        bid_px = math.floor(fair - width)
        if best_bid is not None:
            if best_ask is not None and best_ask - best_bid > 2:
                bid_px = max(bid_px, best_bid + 1)
            else:
                bid_px = max(bid_px, best_bid)
        if best_ask is not None:
            bid_px = min(bid_px, best_ask - 1)
        return bid_px if best_ask is None or bid_px < best_ask else None

    def _passive_ask_price(
        self,
        fair: float,
        width: float,
        best_bid: Optional[int],
        best_ask: Optional[int],
    ) -> Optional[int]:
        ask_px = math.ceil(fair + width)
        if best_ask is not None:
            if best_bid is not None and best_ask - best_bid > 2:
                ask_px = min(ask_px, best_ask - 1)
            else:
                ask_px = min(ask_px, best_ask)
        if best_bid is not None:
            ask_px = max(ask_px, best_bid + 1)
        return ask_px if best_bid is None or ask_px > best_bid else None
