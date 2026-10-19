"""
브로커 인터페이스. 메서드는 이 여섯 개뿐이다 — 이체·입출금 같은 자금 이동 메서드는 정의하지 않는다
(EXECUTOR.md: executor는 돈을 옮기지 않는다. tests/test_executor_guards.py가 정적으로 확인).
list_orders(조회 전용)는 2026-10-19에 재시작 복구(executor/recovery.py)용으로 추가했다.

2026-10-18 기준 구현체는 FakeBroker(테스트용)뿐이다. 실제 자금 계좌용 구현체는 만들지 않기로 했다
(모의투자까지만 범위 — CLAUDE.md 원칙 2·보안 6번과의 조율 결과, EXECUTOR.md 참고).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class BrokerError(Exception):
    """네트워크·API 오류. 메시지에 키·토큰·계좌번호를 넣지 않는다."""


@dataclass(frozen=True)
class Balance:
    total_value: float  # 계좌 평가금액(현금 + 보유 평가액)
    cash: float


@dataclass(frozen=True)
class OrderStatus:
    broker_order_id: str
    status: str  # 'submitted' | 'filled' | 'partial' | 'rejected' | 'cancelled'
    filled_qty: int
    avg_price: float | None
    message: str = ""


@dataclass(frozen=True)
class BrokerOrder:
    """브로커가 알고 있는 주문 하나(재시작 복구용 조회 결과)."""
    broker_order_id: str
    ticker: str
    side: str  # 'buy' | 'sell'
    qty: int
    filled_qty: int
    avg_price: float | None
    open_qty: int  # 아직 살아 있는(체결도 취소도 안 된) 수량. 0이면 끝난 주문
    status: str  # 'submitted' | 'filled' | 'partial' | 'rejected' | 'cancelled'


class Broker(ABC):
    account_kind: str  # 'virtual' | 'real'
    # True면 브로커 상태가 곧 executor 원장이다(섀도) — 원장에 흔적이 없는 주문은 실행되지 않은 것으로 확정할 수 있다.
    ledger_is_broker: bool = False

    @abstractmethod
    def get_balance(self) -> Balance: ...

    @abstractmethod
    def get_positions(self) -> dict[str, int]: ...

    @abstractmethod
    def place_order(self, ticker: str, side: str, qty: int, ref_price: float) -> OrderStatus: ...

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> OrderStatus: ...

    @abstractmethod
    def get_order_status(self, broker_order_id: str) -> OrderStatus: ...

    @abstractmethod
    def list_orders(self, since_date: str) -> list[BrokerOrder]:
        """since_date(YYYY-MM-DD) 이후 이 계좌의 주문 전부(미체결·체결·취소). 조회 전용."""
