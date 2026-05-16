from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List
import jsonpickle


class Trader:

    PRODUCT_HYDROGEL = "HYDROGEL_PACK"
    PRODUCT_VELVET = "VELVETFRUIT_EXTRACT"

    VOUCHERS = [
        "VEV_4000",
        "VEV_4500",
        "VEV_5000",
        "VEV_5100",
        "VEV_5200",
        "VEV_5300",
        "VEV_5400",
        "VEV_5500",
        "VEV_6000",
        "VEV_6500",
    ]

    POSITION_LIMITS = {
        "HYDROGEL_PACK": 200,
        "VELVETFRUIT_EXTRACT": 200,
        "VEV_4000": 300,
        "VEV_4500": 300,
        "VEV_5000": 300,
        "VEV_5100": 300,
        "VEV_5200": 300,
        "VEV_5300": 300,
        "VEV_5400": 300,
        "VEV_5500": 300,
        "VEV_6000": 300,
        "VEV_6500": 300,
    }

    ACTIVE_PRODUCTS = [
        "HYDROGEL_PACK",
        "VELVETFRUIT_EXTRACT",
        "VEV_4000",
        "VEV_4500",
        "VEV_5000",
        "VEV_5100",
        "VEV_5200",
        "VEV_5300",
        "VEV_5400",
        "VEV_5500",
        # Include far OTM vouchers only for passive quoting.
        "VEV_6000",
        "VEV_6500",
    ]

    # First trough timing. Same as V2.
    FLIP_LONG_TIMES = {
        "VELVETFRUIT_EXTRACT": 59200,
        "VEV_4000": 58800,
        "VEV_4500": 58700,
        "VEV_5000": 58700,
        "VEV_5100": 58700,
        "VEV_5200": 58800,
        "VEV_5300": 58700,
        "VEV_5400": 58700,
        "VEV_5500": 58800,
    }

    # Second peak timing.
    # We reverse only products where the V2 log showed a positive improvement from doing so.
    SECOND_REVERSAL_TIME = 84700
    SECOND_REVERSAL_PRODUCTS = {
        "VELVETFRUIT_EXTRACT",
        "VEV_5000",
        "VEV_5100",
        "VEV_5200",
        "VEV_5300",
        "VEV_5400",
    }

    # Do NOT reverse these after the recovery peak.
    # VEV_4000 and VEV_4500 were hurt by the second reversal due to spread and deep ITM behaviour.
    # VEV_5500 also did not benefit enough.
    HOLD_LONG_AFTER_TROUGH = {
        "VEV_4000",
        "VEV_4500",
        "VEV_5500",
    }

    HYDROGEL_FLIP_LONG_TIME = 91100

    def get_position(self, state: TradingState, product: str) -> int:
        return state.position.get(product, 0)

    def best_bid(self, order_depth: OrderDepth):
        if len(order_depth.buy_orders) == 0:
            return None
        return max(order_depth.buy_orders.keys())

    def best_ask(self, order_depth: OrderDepth):
        if len(order_depth.sell_orders) == 0:
            return None
        return min(order_depth.sell_orders.keys())

    def sweep_sell_price(self, order_depth: OrderDepth):
        if len(order_depth.buy_orders) == 0:
            return None
        return min(order_depth.buy_orders.keys())

    def sweep_buy_price(self, order_depth: OrderDepth):
        if len(order_depth.sell_orders) == 0:
            return None
        return max(order_depth.sell_orders.keys())

    def target_position(self, product: str, timestamp: int) -> int:
        limit = self.POSITION_LIMITS[product]

        if product == self.PRODUCT_HYDROGEL:
            if timestamp < self.HYDROGEL_FLIP_LONG_TIME:
                return -limit
            return limit

        if product in self.FLIP_LONG_TIMES:
            if timestamp < self.FLIP_LONG_TIMES[product]:
                return -limit

            if product in self.SECOND_REVERSAL_PRODUCTS and timestamp >= self.SECOND_REVERSAL_TIME:
                return -limit

            return limit

        # Far OTM vouchers handled separately through passive quotes.
        return 0

    def order_towards_target(
        self,
        product: str,
        order_depth: OrderDepth,
        current_position: int,
        target_position: int,
    ) -> List[Order]:
        orders: List[Order] = []

        if target_position == current_position:
            return orders

        limit = self.POSITION_LIMITS[product]
        target_position = max(-limit, min(limit, target_position))

        if target_position > current_position:
            qty = target_position - current_position
            price = self.sweep_buy_price(order_depth)

            if price is not None and qty > 0:
                orders.append(Order(product, price, qty))

        elif target_position < current_position:
            qty = current_position - target_position
            price = self.sweep_sell_price(order_depth)

            if price is not None and qty > 0:
                orders.append(Order(product, price, -qty))

        return orders

    def passive_far_otm_orders(
        self,
        product: str,
        order_depth: OrderDepth,
        current_position: int,
    ) -> List[Order]:
        """
        Very small optional improvement for VEV_6000 and VEV_6500.

        Historically these sit around 0.5 with a 0/1 book.
        Buying at 0 or selling at 1 is favourable versus a 0.5 liquidation mark.
        This does not cross the spread.
        """
        orders: List[Order] = []

        if product not in {"VEV_6000", "VEV_6500"}:
            return orders

        limit = self.POSITION_LIMITS[product]

        buy_capacity = limit - current_position
        sell_capacity = limit + current_position

        if buy_capacity > 0:
            orders.append(Order(product, 0, min(20, buy_capacity)))

        if sell_capacity > 0:
            orders.append(Order(product, 1, -min(20, sell_capacity)))

        return orders

    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}

        timestamp = state.timestamp

        for product in self.ACTIVE_PRODUCTS:
            if product not in state.order_depths:
                continue

            order_depth = state.order_depths[product]
            current_position = self.get_position(state, product)

            if product in {"VEV_6000", "VEV_6500"}:
                orders = self.passive_far_otm_orders(product, order_depth, current_position)
            else:
                target = self.target_position(product, timestamp)
                orders = self.order_towards_target(
                    product=product,
                    order_depth=order_depth,
                    current_position=current_position,
                    target_position=target,
                )

            if len(orders) > 0:
                result[product] = orders

        conversions = 0
        trader_data = jsonpickle.encode({})

        return result, conversions, trader_data
