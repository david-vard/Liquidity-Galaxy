from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List, Tuple


class Trader:
    POSITION_LIMITS: Dict[str, int] = {
        "EMERALDS": 80,
        "TOMATOES": 80,
    }

    PRODUCT_PARAMS: Dict[str, Dict[str, float]] = {
        "EMERALDS": {
            # Inventory control
            "inventory_penalty": 0.08,

            # Signal thresholds on absolute depth-2 imbalance
            # EMERALDS is cleaner, so simpler tiers
            "lean_threshold": 0.10,
            "strong_threshold": 0.20,

            # Quote skew in ticks
            "lean_skew": 1.0,
            "strong_skew": 2.0,

            # Aggressive execution thresholds
            "aggressive_edge": 1.5,   # best quote must beat reservation price by this much
            "passive_edge": 0.0,

            # Sizes
            "base_passive_size": 8,
            "base_aggressive_size": 12,

            # Inventory zone cutoffs
            "comfort_pos": 20,
            "caution_pos": 40,
            "defensive_pos": 60,
        },
        "TOMATOES": {
            # Stronger inventory penalty because more active/noisy
            "inventory_penalty": 0.15,

            # TOMATOES needs thresholding
            "lean_threshold": 0.12,
            "strong_threshold": 0.28,

            # Quote skew in ticks
            "lean_skew": 1.0,
            "strong_skew": 2.0,

            # Aggressive execution thresholds
            "aggressive_edge": 2.0,
            "passive_edge": 0.0,

            # Sizes
            "base_passive_size": 5,
            "base_aggressive_size": 7,

            # Inventory zone cutoffs
            "comfort_pos": 20,
            "caution_pos": 40,
            "defensive_pos": 60,
        },
    }

    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}

        for product, order_depth in state.order_depths.items():
            if product not in self.POSITION_LIMITS:
                continue

            position = state.position.get(product, 0)
            orders = self.trade_product(product, order_depth, position)
            result[product] = orders

        conversions = 0
        trader_data = ""
        return result, conversions, trader_data

    # =========================
    # Core product logic
    # =========================

    def trade_product(
        self,
        product: str,
        order_depth: OrderDepth,
        position: int,
    ) -> List[Order]:
        params = self.PRODUCT_PARAMS[product]
        orders: List[Order] = []

        best_bid, best_bid_vol, best_ask, best_ask_vol = self.get_top_of_book(order_depth)
        if best_bid is None or best_ask is None:
            return orders

        mid = (best_bid + best_ask) / 2.0

        d2_imbalance = self.compute_depth2_imbalance(order_depth)
        fair_value = self.compute_depth2_fair_value(order_depth, mid)
        reservation_price = fair_value - params["inventory_penalty"] * position

        signal_tier, signal_dir = self.classify_signal(
            d2_imbalance,
            params["lean_threshold"],
            params["strong_threshold"],
        )

        buy_capacity, sell_capacity = self.get_capacities(product, position)

        # Inventory zone controls sizing and whether same-side aggression is allowed
        zone = self.inventory_zone(product, position)
        passive_buy_size, passive_sell_size, aggressive_buy_allowed, aggressive_sell_allowed = (
            self.inventory_controls(product, position, zone)
        )

        # -------------------------
        # 1. Selective aggressive execution
        # -------------------------
        # Buy aggressively only if bullish signal is strong and ask is attractive.
        if signal_dir > 0 and signal_tier == "strong" and aggressive_buy_allowed and buy_capacity > 0:
            buy_edge = reservation_price - best_ask
            if buy_edge >= params["aggressive_edge"]:
                qty = min(params["base_aggressive_size"], passive_buy_size, best_ask_vol, buy_capacity)
                if qty > 0:
                    orders.append(Order(product, best_ask, qty))
                    buy_capacity -= qty

        # Sell aggressively only if bearish signal is strong and bid is attractive.
        if signal_dir < 0 and signal_tier == "strong" and aggressive_sell_allowed and sell_capacity > 0:
            sell_edge = best_bid - reservation_price
            if sell_edge >= params["aggressive_edge"]:
                qty = min(params["base_aggressive_size"], passive_sell_size, best_bid_vol, sell_capacity)
                if qty > 0:
                    orders.append(Order(product, best_bid, -qty))
                    sell_capacity -= qty

        # -------------------------
        # 2. Passive quoting
        # -------------------------
        bid_quote, ask_quote = self.compute_passive_quotes(
            product=product,
            best_bid=best_bid,
            best_ask=best_ask,
            reservation_price=reservation_price,
            signal_tier=signal_tier,
            signal_dir=signal_dir,
        )

        # Passive bid
        if buy_capacity > 0 and passive_buy_size > 0:
            # Only quote a bid if it makes sense relative to reservation price.
            # We also cap it so we do not cross accidentally.
            max_bid_price = best_ask - 1
            bid_px = min(bid_quote, max_bid_price)
            if bid_px >= best_bid:
                qty = min(params["base_passive_size"], passive_buy_size, buy_capacity)
                if qty > 0:
                    orders.append(Order(product, int(round(bid_px)), qty))

        # Passive ask
        if sell_capacity > 0 and passive_sell_size > 0:
            # Only quote an ask if it makes sense relative to reservation price.
            # We also cap it so we do not cross accidentally.
            min_ask_price = best_bid + 1
            ask_px = max(ask_quote, min_ask_price)
            if ask_px <= best_ask:
                qty = min(params["base_passive_size"], passive_sell_size, sell_capacity)
                if qty > 0:
                    orders.append(Order(product, int(round(ask_px)), -qty))

        # Final hard safety check: ensure aggregate side size is legal
        orders = self.enforce_side_limits(product, position, orders)
        return orders

    # =========================
    # Market state helpers
    # =========================

    def get_top_of_book(
        self,
        order_depth: OrderDepth,
    ) -> Tuple[int, int, int, int]:
        if not order_depth.buy_orders or not order_depth.sell_orders:
            return None, None, None, None

        best_bid = max(order_depth.buy_orders.keys())
        best_ask = min(order_depth.sell_orders.keys())

        best_bid_vol = order_depth.buy_orders[best_bid]
        best_ask_vol = -order_depth.sell_orders[best_ask]

        return best_bid, best_bid_vol, best_ask, best_ask_vol

    def compute_depth2_imbalance(self, order_depth: OrderDepth) -> float:
        """
        Signed imbalance using levels 1 and 2:
        (bid_depth_1_2 - ask_depth_1_2) / total_depth_1_2
        """
        buy_levels = sorted(order_depth.buy_orders.items(), key=lambda x: x[0], reverse=True)[:2]
        sell_levels = sorted(order_depth.sell_orders.items(), key=lambda x: x[0])[:2]

        bid_depth = sum(vol for _, vol in buy_levels)
        ask_depth = sum(-vol for _, vol in sell_levels)

        total = bid_depth + ask_depth
        if total <= 0:
            return 0.0

        return (bid_depth - ask_depth) / total

    def compute_depth2_fair_value(self, order_depth: OrderDepth, fallback_mid: float) -> float:
        """
        Depth-2 weighted fair value:
        Use price*opposite-side-volume weighting across the top 2 levels on each side.
        """
        buy_levels = sorted(order_depth.buy_orders.items(), key=lambda x: x[0], reverse=True)[:2]
        sell_levels = sorted(order_depth.sell_orders.items(), key=lambda x: x[0])[:2]

        if not buy_levels or not sell_levels:
            return fallback_mid

        bid_depth = sum(vol for _, vol in buy_levels)
        ask_depth = sum(-vol for _, vol in sell_levels)

        if bid_depth <= 0 or ask_depth <= 0:
            return fallback_mid

        weighted_bid = sum(price * vol for price, vol in buy_levels) / bid_depth
        weighted_ask = sum(price * (-vol) for price, vol in sell_levels) / ask_depth

        fair_value = (weighted_ask * bid_depth + weighted_bid * ask_depth) / (bid_depth + ask_depth)
        return fair_value

    def classify_signal(
        self,
        imbalance: float,
        lean_threshold: float,
        strong_threshold: float,
    ) -> Tuple[str, int]:
        """
        Returns:
            signal_tier in {"neutral", "lean", "strong"}
            signal_dir in {-1, 0, 1}
        """
        if abs(imbalance) < lean_threshold:
            return "neutral", 0
        if abs(imbalance) < strong_threshold:
            return "lean", 1 if imbalance > 0 else -1
        return "strong", 1 if imbalance > 0 else -1

    # =========================
    # Inventory and risk helpers
    # =========================

    def get_capacities(self, product: str, position: int) -> Tuple[int, int]:
        limit = self.POSITION_LIMITS[product]
        buy_capacity = max(0, limit - position)
        sell_capacity = max(0, limit + position)
        return buy_capacity, sell_capacity

    def inventory_zone(self, product: str, position: int) -> str:
        params = self.PRODUCT_PARAMS[product]
        abs_pos = abs(position)

        if abs_pos <= params["comfort_pos"]:
            return "comfort"
        if abs_pos <= params["caution_pos"]:
            return "caution"
        if abs_pos <= params["defensive_pos"]:
            return "defensive"
        return "near_limit"

    def inventory_controls(
        self,
        product: str,
        position: int,
        zone: str,
    ) -> Tuple[int, int, bool, bool]:
        """
        Returns:
            passive_buy_size
            passive_sell_size
            aggressive_buy_allowed
            aggressive_sell_allowed
        """
        params = self.PRODUCT_PARAMS[product]
        base_passive = int(params["base_passive_size"])

        # Start with symmetric sizes
        buy_size = base_passive
        sell_size = base_passive

        aggressive_buy_allowed = True
        aggressive_sell_allowed = True

        # If long, buying is the risky side.
        # If short, selling is the risky side.
        if zone == "comfort":
            pass

        elif zone == "caution":
            if position > 0:
                buy_size = max(1, int(base_passive * 0.6))
            elif position < 0:
                sell_size = max(1, int(base_passive * 0.6))

        elif zone == "defensive":
            if position > 0:
                buy_size = max(1, int(base_passive * 0.25))
                aggressive_buy_allowed = False
            elif position < 0:
                sell_size = max(1, int(base_passive * 0.25))
                aggressive_sell_allowed = False

        elif zone == "near_limit":
            if position > 0:
                buy_size = 0
                aggressive_buy_allowed = False
            elif position < 0:
                sell_size = 0
                aggressive_sell_allowed = False

        return buy_size, sell_size, aggressive_buy_allowed, aggressive_sell_allowed

    def enforce_side_limits(
        self,
        product: str,
        position: int,
        orders: List[Order],
    ) -> List[Order]:
        """
        Final safety clamp so aggregate buy/sell submitted size cannot breach legal capacity.
        """
        buy_capacity, sell_capacity = self.get_capacities(product, position)

        safe_orders: List[Order] = []
        used_buy = 0
        used_sell = 0

        for order in orders:
            if order.quantity > 0:
                remaining = buy_capacity - used_buy
                if remaining <= 0:
                    continue
                qty = min(order.quantity, remaining)
                if qty > 0:
                    safe_orders.append(Order(order.symbol, order.price, qty))
                    used_buy += qty

            elif order.quantity < 0:
                remaining = sell_capacity - used_sell
                if remaining <= 0:
                    continue
                qty = min(-order.quantity, remaining)
                if qty > 0:
                    safe_orders.append(Order(order.symbol, order.price, -qty))
                    used_sell += qty

        return safe_orders

    # =========================
    # Quote placement helpers
    # =========================

    def compute_passive_quotes(
        self,
        product: str,
        best_bid: int,
        best_ask: int,
        reservation_price: float,
        signal_tier: str,
        signal_dir: int,
    ) -> Tuple[float, float]:
        """
        Returns passive bid and ask quote prices.
        We start from reservation price and skew based on signal.
        """
        params = self.PRODUCT_PARAMS[product]
        spread = best_ask - best_bid

        # Base quotes around reservation price
        # Keep them inside the spread when possible
        base_bid = min(best_bid + 1, reservation_price - 0.5)
        base_ask = max(best_ask - 1, reservation_price + 0.5)

        if signal_tier == "neutral":
            return base_bid, base_ask

        if signal_tier == "lean":
            skew = params["lean_skew"]
        else:
            skew = params["strong_skew"]

        if signal_dir > 0:
            # Bullish: quote bid more aggressively, ask less eagerly
            bid_quote = base_bid + skew
            ask_quote = base_ask + skew
        else:
            # Bearish: quote ask more aggressively, bid less eagerly
            bid_quote = base_bid - skew
            ask_quote = base_ask - skew

        # Keep quotes sensible relative to the current spread
        bid_quote = min(bid_quote, best_ask - 1)
        ask_quote = max(ask_quote, best_bid + 1)

        return bid_quote, ask_quote