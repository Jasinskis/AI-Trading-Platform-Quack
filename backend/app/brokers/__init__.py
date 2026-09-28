from __future__ import annotations

from app.brokers.base import Broker, MockBroker
from app.brokers.trading212 import Trading212Broker
from app.config import get_settings


async def build_broker(starting_cash: float = 1000.0, *, force_simulate: bool = False) -> Broker:
    settings = get_settings()
    if force_simulate or settings.mock_broker or not settings.t212_api_key:
        broker = MockBroker(starting_cash=starting_cash)
        await broker.connect()
        return broker
    broker = Trading212Broker()
    ok = await broker.connect()
    if not ok:
        mock = MockBroker(starting_cash=starting_cash)
        await mock.connect()
        return mock
    return broker
