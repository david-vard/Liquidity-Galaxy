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

    STRIKES = {
        "VEV_4000": 4000,
        "VEV_4500": 4500,
        "VEV_5000": 5000,
        "VEV_5100": 5100,
        "VEV_5200": 5200,
        "VEV_5300": 5300,
        "VEV_5400": 5400,
        "VEV_5500": 5500,
        "VEV_6000": 6000,
        "VEV_6500": 6500,
    }

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

    TRADABLE_VOUCHERS = [
        "VEV_5000",
        "VEV_5100",
        "VEV_5200",
        "VEV_5300",
        "VEV_5400",
        "VEV_5500",
    ]

    VOUCHER_THRESHOLD = 12
    VOUCHER_BASE_SIZE = 10
    SOFT_DELTA_LIMIT = 200

    TTE5_TIME_VALUE = {
        "VEV_4000": 0.003,
        "VEV_4500": 0.0098,
        "VEV_5000": 1.3276,
        "VEV_5100": 7.0432,
        "VEV_5200": 33.1445,
        "VEV_5300": 42.3455,
        "VEV_5400": 11.219,
        "VEV_5500": 3.8766,
        "VEV_6000": 0.5,
        "VEV_6500": 0.5
}

    EMPIRICAL_DELTAS = {
        "VEV_4000": 0.9997,
        "VEV_4500": 0.9994,
        "VEV_5000": 0.9153,
        "VEV_5100": 0.7843,
        "VEV_5200": 0.5651,
        "VEV_5300": 0.3336,
        "VEV_5400": 0.1257,
        "VEV_5500": 0.0549,
        "VEV_6000": 0.0,
        "VEV_6500": 0.0
}

    TRADE_DELTA_ONE = False

    DELTA_ONE_BASE_SIZE = {
        "HYDROGEL_PACK": 10,
        "VELVETFRUIT_EXTRACT": 10,
    }

    DELTA_ONE_THRESHOLD = {
        "HYDROGEL_PACK": 20,
        "VELVETFRUIT_EXTRACT": 8,
    }

    def get_best_bid_ask(self, order_depth: OrderDepth):
        if len(order_depth.buy_orders) == 0 or len(order_depth.sell_orders) == 0:
            return None, None

        best_bid = max(order_depth.buy_orders.keys())
        best_ask = min(order_depth.sell_orders.keys())
        return best_bid, best_ask

    def get_mid_price(self, order_depth: OrderDepth):
        best_bid, best_ask = self.get_best_bid_ask(order_depth)
        if best_bid is None or best_ask is None:
            return None
        return (best_bid + best_ask) / 2

    def estimate_voucher_fair_value(self, voucher: str, underlying_mid: float) -> float:
        strike = self.STRIKES[voucher]
        intrinsic = max(underlying_mid - strike, 0)
        time_value = self.TTE5_TIME_VALUE.get(voucher, 0)
        return intrinsic + time_value

    def inventory_scaled_size(self, base_size: int, position: int, limit: int) -> int:
        scale = max(0.0, 1.0 - abs(position) / limit)
        return max(1, int(round(base_size * scale)))

    def edge_scaled_size(self, edge: float, threshold: float, base_size: int, max_size: int = 30) -> int:
        if edge <= threshold:
            return 0

        edge_multiple = edge / threshold
        size = int(round(base_size * min(edge_multiple, 3.0)))
        return max(1, min(size, max_size))

    def capacity_for_buy(self, position: int, limit: int) -> int:
        return max(0, limit - position)

    def capacity_for_sell(self, position: int, limit: int) -> int:
        return max(0, limit + position)

    def estimate_portfolio_delta(self, positions: Dict[str, int]) -> float:
        total_delta = positions.get(self.PRODUCT_VELVET, 0)

        for voucher, delta in self.EMPIRICAL_DELTAS.items():
            total_delta += positions.get(voucher, 0) * delta

        return total_delta

    def voucher_order(
        self,
        voucher: str,
        order_depth: OrderDepth,
        underlying_mid: float,
        position: int,
        portfolio_delta: float,
    ) -> List[Order]:
        orders: List[Order] = []

        if voucher not in self.TRADABLE_VOUCHERS:
            return orders

        best_bid, best_ask = self.get_best_bid_ask(order_depth)
        if best_bid is None or best_ask is None:
            return orders

        fair_value = self.estimate_voucher_fair_value(voucher, underlying_mid)

        buy_edge = fair_value - best_ask
        sell_edge = best_bid - fair_value

        limit = self.POSITION_LIMITS[voucher]
        inv_size = self.inventory_scaled_size(self.VOUCHER_BASE_SIZE, position, limit)

        if buy_edge > self.VOUCHER_THRESHOLD:
            if portfolio_delta < self.SOFT_DELTA_LIMIT:
                raw_size = self.edge_scaled_size(
                    buy_edge,
                    self.VOUCHER_THRESHOLD,
                    inv_size,
                    max_size=30,
                )
                quantity = min(raw_size, self.capacity_for_buy(position, limit))

                if quantity > 0:
                    orders.append(Order(voucher, best_ask, quantity))

        elif sell_edge > self.VOUCHER_THRESHOLD:
            if portfolio_delta > -self.SOFT_DELTA_LIMIT:
                raw_size = self.edge_scaled_size(
                    sell_edge,
                    self.VOUCHER_THRESHOLD,
                    inv_size,
                    max_size=30,
                )
                quantity = min(raw_size, self.capacity_for_sell(position, limit))

                if quantity > 0:
                    orders.append(Order(voucher, best_bid, -quantity))

        return orders

    def conservative_delta_one_order(
        self,
        product: str,
        order_depth: OrderDepth,
        position: int,
    ) -> List[Order]:
        orders: List[Order] = []

        if not self.TRADE_DELTA_ONE:
            return orders

        best_bid, best_ask = self.get_best_bid_ask(order_depth)
        if best_bid is None or best_ask is None:
            return orders

        fair_value = (best_bid + best_ask) / 2

        threshold = self.DELTA_ONE_THRESHOLD[product]
        base_size = self.DELTA_ONE_BASE_SIZE[product]
        limit = self.POSITION_LIMITS[product]

        buy_edge = fair_value - best_ask
        sell_edge = best_bid - fair_value

        size = self.inventory_scaled_size(base_size, position, limit)

        if buy_edge > threshold:
            quantity = min(size, self.capacity_for_buy(position, limit))
            if quantity > 0:
                orders.append(Order(product, best_ask, quantity))

        elif sell_edge > threshold:
            quantity = min(size, self.capacity_for_sell(position, limit))
            if quantity > 0:
                orders.append(Order(product, best_bid, -quantity))

        return orders

    def run(self, state: TradingState):
        result: Dict[str, List[Order]] = {}

        positions = dict(state.position)

        velvet_depth = state.order_depths.get(self.PRODUCT_VELVET)

        if velvet_depth is None:
            trader_data = jsonpickle.encode({})
            return result, 0, trader_data

        underlying_mid = self.get_mid_price(velvet_depth)

        if underlying_mid is None:
            trader_data = jsonpickle.encode({})
            return result, 0, trader_data

        portfolio_delta = self.estimate_portfolio_delta(positions)

        for product in [self.PRODUCT_HYDROGEL, self.PRODUCT_VELVET]:
            if product in state.order_depths:
                position = positions.get(product, 0)
                orders = self.conservative_delta_one_order(
                    product,
                    state.order_depths[product],
                    position,
                )
                if len(orders) > 0:
                    result[product] = orders

        for voucher in self.TRADABLE_VOUCHERS:
            if voucher not in state.order_depths:
                continue

            position = positions.get(voucher, 0)
            orders = self.voucher_order(
                voucher=voucher,
                order_depth=state.order_depths[voucher],
                underlying_mid=underlying_mid,
                position=position,
                portfolio_delta=portfolio_delta,
            )

            if len(orders) > 0:
                result[voucher] = orders

                signed_qty = sum(order.quantity for order in orders)
                portfolio_delta += signed_qty * self.EMPIRICAL_DELTAS.get(voucher, 0)

        trader_data = jsonpickle.encode({})
        conversions = 0

        return result, conversions, trader_data