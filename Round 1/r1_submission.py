from datamodel import Order, OrderDepth, TradingState
from typing import Dict, List
import json
import math


class Trader:
    POSITION_LIMITS: Dict[str, int] = {
        'ASH_COATED_OSMIUM': 80,
        'INTARIAN_PEPPER_ROOT': 80,
    }

    def load_state(self, trader_data: str) -> Dict:
        if trader_data:
            try:
                return json.loads(trader_data)
            except Exception:
                return {}
        return {}

    def dump_state(self, state: Dict) -> str:
        return json.dumps(state)

    def best_bid_ask(self, order_depth: OrderDepth):
        best_bid = max(order_depth.buy_orders) if order_depth.buy_orders else None
        best_ask = min(order_depth.sell_orders) if order_depth.sell_orders else None

        bid_vol = order_depth.buy_orders.get(best_bid, 0) if best_bid is not None else 0
        ask_vol = -order_depth.sell_orders.get(best_ask, 0) if best_ask is not None else 0

        return best_bid, bid_vol, best_ask, ask_vol

    def place_buy(self, orders: List[Order], product: str, price: int, size: int):
        if size > 0:
            orders.append(Order(product, int(price), int(size)))

    def place_sell(self, orders: List[Order], product: str, price: int, size: int):
        if size > 0:
            orders.append(Order(product, int(price), int(-size)))

    def l1_imbalance(self, bid_vol: int, ask_vol: int) -> float:
        total = bid_vol + ask_vol
        if total <= 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    def update_pepper_base(self, memory: Dict, timestamp: int, mid: float) -> float:
        observed_base = mid - timestamp / 1000.0
        previous_base = memory.get('pepper_base')

        if previous_base is None:
            base = round(observed_base / 1000.0) * 1000.0
        else:
            base = 0.90 * float(previous_base) + 0.10 * observed_base

        memory['pepper_base'] = base
        return base

    def pepper_target_position(self, timestamp: int) -> int:
        # Mild structural long bias that fades into the close.
        # Early day target is higher, then decays gradually.
        if timestamp < 200000:
            return 24
        if timestamp < 400000:
            return 20
        if timestamp < 600000:
            return 16
        if timestamp < 800000:
            return 10
        if timestamp < 920000:
            return 4
        return 0

    def trade_osmium(self, product: str, order_depth: OrderDepth, position: int, timestamp: int) -> List[Order]:
        orders: List[Order] = []
        limit = self.POSITION_LIMITS[product]

        best_bid, bid_vol, best_ask, ask_vol = self.best_bid_ask(order_depth)
        if best_bid is None or best_ask is None:
            return orders

        fair = 10000.0
        imbalance = self.l1_imbalance(bid_vol, ask_vol)

        inventory_penalty = 0.10
        imbalance_lean = 2.5 * imbalance
        reservation = fair + imbalance_lean - inventory_penalty * position

        buy_room = limit - position
        sell_room = limit + position

        aggressive_buy_edge = fair - best_ask
        aggressive_sell_edge = best_bid - fair

        if aggressive_buy_edge >= 2 and imbalance >= -0.15 and buy_room > 0:
            size = min(buy_room, ask_vol, 12)
            self.place_buy(orders, product, best_ask, size)
            buy_room -= size

        if aggressive_sell_edge >= 2 and imbalance <= 0.15 and sell_room > 0:
            size = min(sell_room, bid_vol, 12)
            self.place_sell(orders, product, best_bid, size)
            sell_room -= size

        bid_quote = min(best_bid + 1, math.floor(reservation - 1))
        ask_quote = max(best_ask - 1, math.ceil(reservation + 1))

        if bid_quote < ask_quote:
            base_passive = 10
            buy_size = min(buy_room, max(0, base_passive - max(position, 0) // 10))
            sell_size = min(sell_room, max(0, base_passive - max(-position, 0) // 10))

            if buy_size > 0:
                self.place_buy(orders, product, bid_quote, buy_size)
            if sell_size > 0:
                self.place_sell(orders, product, ask_quote, sell_size)

        if timestamp >= 950000:
            if position > 0 and sell_room > 0:
                size = min(position, sell_room, max(1, bid_vol))
                self.place_sell(orders, product, best_bid, size)
            elif position < 0 and buy_room > 0:
                size = min(-position, buy_room, max(1, ask_vol))
                self.place_buy(orders, product, best_ask, size)

        return orders

    def trade_pepper(self, product: str, order_depth: OrderDepth, position: int, timestamp: int, memory: Dict) -> List[Order]:
        orders: List[Order] = []
        limit = self.POSITION_LIMITS[product]

        best_bid, bid_vol, best_ask, ask_vol = self.best_bid_ask(order_depth)
        if best_bid is None or best_ask is None:
            return orders

        mid = 0.5 * (best_bid + best_ask)
        imbalance = self.l1_imbalance(bid_vol, ask_vol)

        base = self.update_pepper_base(memory, timestamp, mid)
        fair = base + timestamp / 1000.0

        target_pos = self.pepper_target_position(timestamp)
        target_gap = target_pos - position

        # Mild upward drift bias:
        # actual inventory still matters, but being below target should pull reservation up.
        inventory_penalty = 0.09
        target_penalty = 0.07
        imbalance_lean = 1.1 * imbalance

        reservation = (
            fair
            + imbalance_lean
            - inventory_penalty * position
            + target_penalty * target_gap
        )

        buy_room = limit - position
        sell_room = limit + position

        fair_premium = fair - mid
        buy_edge = fair - best_ask
        sell_edge = best_bid - fair

        under_target = position < target_pos
        far_under_target = position < target_pos - 10
        over_target = position > target_pos + 8

        # Easier to buy than sell, especially when below target.
        buy_threshold = 0.5 if under_target else 1.0
        sell_threshold = 1.75 if under_target else 1.0

        # Aggressive buys
        if buy_edge >= buy_threshold and imbalance >= -0.50 and buy_room > 0:
            max_take = 16
            if buy_edge >= 1.5:
                max_take = 22
            if buy_edge >= 2.5:
                max_take = 28
            if far_under_target:
                max_take += 8

            size = min(buy_room, ask_vol, max_take)
            self.place_buy(orders, product, best_ask, size)
            buy_room -= size

        # Opportunistic target-building buy even if strict edge is small
        if under_target and fair_premium >= 0.5 and buy_room > 0 and best_ask <= math.floor(reservation):
            size = min(buy_room, ask_vol, 8 if not far_under_target else 12)
            self.place_buy(orders, product, best_ask, size)
            buy_room -= size

        # Aggressive sells
        if sell_edge >= sell_threshold and imbalance <= 0.20 and sell_room > 0:
            max_take = 12
            if sell_edge >= 2.0:
                max_take = 18
            if sell_edge >= 3.0:
                max_take = 24
            if over_target:
                max_take += 6

            size = min(sell_room, bid_vol, max_take)
            self.place_sell(orders, product, best_bid, size)
            sell_room -= size

        # Passive quoting with target-long bias
        bid_quote = min(best_bid + 1, math.floor(reservation - 1))
        ask_quote = max(best_ask - 1, math.ceil(reservation + 1))

        if bid_quote < ask_quote:
            base_passive = 12
            if abs(fair_premium) >= 1:
                base_passive = 16

            if under_target:
                buy_size = min(buy_room, base_passive + min(12, max(0, target_gap) // 2 + 4))
                sell_size = min(sell_room, max(0, base_passive - 8))
            elif over_target:
                buy_size = min(buy_room, max(0, base_passive - 8))
                sell_size = min(sell_room, base_passive + min(10, (position - target_pos) // 2 + 2))
            else:
                buy_size = min(buy_room, base_passive)
                sell_size = min(sell_room, base_passive)

            if buy_size > 0:
                self.place_buy(orders, product, bid_quote, buy_size)
            if sell_size > 0:
                self.place_sell(orders, product, ask_quote, sell_size)

        # End-of-day flattening
        if timestamp >= 930000:
            if position > 0 and sell_room > 0:
                size = min(position, sell_room, max(1, bid_vol) + 4)
                self.place_sell(orders, product, best_bid, size)
            elif position < 0 and buy_room > 0:
                size = min(-position, buy_room, max(1, ask_vol) + 4)
                self.place_buy(orders, product, best_ask, size)

        return orders

    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}
        memory = self.load_state(state.traderData)

        for product, order_depth in state.order_depths.items():
            position = state.position.get(product, 0)

            if product == 'ASH_COATED_OSMIUM':
                result[product] = self.trade_osmium(
                    product=product,
                    order_depth=order_depth,
                    position=position,
                    timestamp=state.timestamp,
                )

            elif product == 'INTARIAN_PEPPER_ROOT':
                result[product] = self.trade_pepper(
                    product=product,
                    order_depth=order_depth,
                    position=position,
                    timestamp=state.timestamp,
                    memory=memory,
                )

            else:
                result[product] = []

        trader_data = self.dump_state(memory)
        conversions = 0
        return result, conversions, trader_data