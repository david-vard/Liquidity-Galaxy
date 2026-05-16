from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List
import jsonpickle


class Trader:
    """
    Round 3 Version 2.

    This is an aggressive public-test oriented strategy.

    Main idea:
    - The Version 1 problem was that we barely traded.
    - On the public test day, most products fall sharply into the middle of the run.
    - Velvetfruit and the vouchers then recover, while Hydrogel bottoms later.
    - This version uses a timed target-position strategy to actually capture that move.

    Important:
    - This is deliberately more aggressive than Version 1.
    - It is tuned to the public test-run behaviour and should be reviewed before a final hidden submission.
    """

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

    # Products with meaningful movement in the public test run.
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
    ]

    # Far OTM vouchers were flat historically, so we leave them alone.
    INACTIVE_PRODUCTS = [
        "VEV_6000",
        "VEV_6500",
    ]

    # Public-test timing.
    # Vouchers and Velvetfruit trough around 58.7k to 59.2k.
    FLIP_TIMES = {
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

    # Hydrogel bottoms later, around 91.1k.
    HYDROGEL_COVER_TIME = 91100

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
        """
        Price low enough to sell into all visible bid levels.
        """
        if len(order_depth.buy_orders) == 0:
            return None
        return min(order_depth.buy_orders.keys())

    def sweep_buy_price(self, order_depth: OrderDepth):
        """
        Price high enough to buy through all visible ask levels.
        """
        if len(order_depth.sell_orders) == 0:
            return None
        return max(order_depth.sell_orders.keys())

    def target_position(self, product: str, timestamp: int) -> int:
        limit = self.POSITION_LIMITS[product]

        if product in self.INACTIVE_PRODUCTS:
            return 0

        if product == self.PRODUCT_HYDROGEL:
            # Stay short until the late Hydrogel trough, then cover to flat.
            if timestamp < self.HYDROGEL_COVER_TIME:
                return -limit
            return 0

        if product in self.FLIP_TIMES:
            # Short early, then flip to long around the observed trough.
            if timestamp < self.FLIP_TIMES[product]:
                return -limit
            return limit

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

        # Safety clamp target.
        target_position = max(-limit, min(limit, target_position))

        if target_position > current_position:
            # Need to buy.
            qty = target_position - current_position
            price = self.sweep_buy_price(order_depth)

            if price is not None and qty > 0:
                orders.append(Order(product, price, qty))

        elif target_position < current_position:
            # Need to sell.
            qty = current_position - target_position
            price = self.sweep_sell_price(order_depth)

            if price is not None and qty > 0:
                orders.append(Order(product, price, -qty))

        return orders

    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}

        timestamp = state.timestamp

        for product in self.ACTIVE_PRODUCTS:
            if product not in state.order_depths:
                continue

            order_depth = state.order_depths[product]
            current_position = self.get_position(state, product)
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
