"""
FakeBroker — 테스트용 브로커. 네트워크를 쓰지 않는다.

시뮬레이션:
  - fill_delay_polls: 주문 후 상태 조회를 이 횟수만큼 'submitted'로 돌려준 뒤 체결
  - partial_ratio: {ticker: 0~1} — 그 비율만큼만 체결되고 나머지는 미체결('partial')
  - reject_tickers: 이 종목 주문은 거절('rejected')
  - fail_calls: {'place_order': n, ...} — 해당 메서드의 처음 n번 호출에서 BrokerError
  - external_positions: 브로커 쪽에만 있는 수량(사람이 직접 산 주식 등) — 잔고 불일치 시나리오용
체결 시 수수료(매수·매도)와 매도세를 현금에서 뺀다 — executor 원장과 같은 요율을 써야 대사가 맞는다.
"""

from __future__ import annotations

import itertools

from executor.broker.base import Balance, Broker, BrokerError, BrokerOrder, OrderStatus


class FakeBroker(Broker):
    def __init__(self, account_kind: str = "virtual", cash: float = 0.0, prices: dict[str, float] | None = None,
                 positions: dict[str, int] | None = None, commission_rate: float = 0.00015, tax_rate: float = 0.002,
                 fill_delay_polls: int = 0, partial_ratio: dict[str, float] | None = None,
                 reject_tickers: set[str] | None = None, fail_calls: dict[str, int] | None = None):
        self.account_kind = account_kind
        self.cash = float(cash)
        self.prices = dict(prices or {})
        self.positions = dict(positions or {})
        self.commission_rate, self.tax_rate = commission_rate, tax_rate
        self.fill_delay_polls = fill_delay_polls
        self.partial_ratio = dict(partial_ratio or {})
        self.reject_tickers = set(reject_tickers or ())
        self.fail_calls = dict(fail_calls or {})
        self.calls: list[tuple] = []
        self._orders: dict[str, dict] = {}
        self._ids = itertools.count(1)

    def _maybe_fail(self, name: str) -> None:
        self.calls.append((name,))
        if self.fail_calls.get(name, 0) > 0:
            self.fail_calls[name] -= 1
            raise BrokerError(f"{name}: 네트워크 오류(시뮬레이션)")

    def get_balance(self) -> Balance:
        self._maybe_fail("get_balance")
        value = self.cash + sum(q * self.prices[t] for t, q in self.positions.items() if q)
        return Balance(total_value=value, cash=self.cash)

    def get_positions(self) -> dict[str, int]:
        self._maybe_fail("get_positions")
        return {t: q for t, q in self.positions.items() if q}

    def place_order(self, ticker: str, side: str, qty: int, ref_price: float) -> OrderStatus:
        self._maybe_fail("place_order")
        oid = f"F{next(self._ids)}"
        o = {"ticker": ticker, "side": side, "qty": int(qty), "filled": 0, "polls": 0, "status": "submitted"}
        self._orders[oid] = o
        if ticker in self.reject_tickers:
            o["status"] = "rejected"
            return OrderStatus(oid, "rejected", 0, None, "거절(시뮬레이션)")
        if side == "sell" and self.positions.get(ticker, 0) < qty:
            o["status"] = "rejected"
            return OrderStatus(oid, "rejected", 0, None, "보유 수량 부족")
        if side == "buy" and qty * self.prices[ticker] * (1 + self.commission_rate) > self.cash + 1e-6:
            o["status"] = "rejected"
            return OrderStatus(oid, "rejected", 0, None, "현금 부족")
        if self.fill_delay_polls == 0:
            self._fill(o)
        return self._status(oid)

    def _fill(self, o: dict) -> None:
        ratio = self.partial_ratio.get(o["ticker"], 1.0)
        qty = int(o["qty"] * ratio)
        price = self.prices[o["ticker"]]
        gross = qty * price
        if o["side"] == "buy":
            self.cash -= gross * (1 + self.commission_rate)
            self.positions[o["ticker"]] = self.positions.get(o["ticker"], 0) + qty
        else:
            self.cash += gross * (1 - self.commission_rate - self.tax_rate)
            self.positions[o["ticker"]] = self.positions.get(o["ticker"], 0) - qty
        o["filled"] = qty
        o["status"] = "filled" if qty == o["qty"] else "partial"

    def _status(self, oid: str) -> OrderStatus:
        o = self._orders[oid]
        avg = self.prices[o["ticker"]] if o["filled"] else None
        return OrderStatus(oid, o["status"], o["filled"], avg)

    def get_order_status(self, broker_order_id: str) -> OrderStatus:
        self._maybe_fail("get_order_status")
        o = self._orders[broker_order_id]
        if o["status"] == "submitted":
            o["polls"] += 1
            if o["polls"] > self.fill_delay_polls:
                self._fill(o)
        return self._status(broker_order_id)

    def cancel_order(self, broker_order_id: str) -> OrderStatus:
        self._maybe_fail("cancel_order")
        o = self._orders[broker_order_id]
        if o["status"] in ("submitted", "partial") and not o.get("closed"):
            o["status"] = "cancelled" if o["filled"] == 0 else "partial"
            o["closed"] = True
        return self._status(broker_order_id)

    def list_orders(self, since_date: str) -> list[BrokerOrder]:
        self._maybe_fail("list_orders")
        out = []
        for oid, o in self._orders.items():
            done = o["status"] in ("filled", "rejected", "cancelled") or o.get("closed")
            out.append(BrokerOrder(oid, o["ticker"], o["side"], o["qty"], o["filled"],
                                   self.prices[o["ticker"]] if o["filled"] else None,
                                   0 if done else o["qty"] - o["filled"], o["status"]))
        return out
