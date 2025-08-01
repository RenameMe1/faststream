import pytest

from faststream.nats import TestApp, TestNatsBroker


@pytest.mark.nats()
@pytest.mark.asyncio()
async def test_pattern() -> None:
    from docs.docs_src.nats.pattern import (
        app,
        base_handler1,
        base_handler2,
        base_handler3,
        broker,
    )

    async with TestNatsBroker(broker), TestApp(app):
        assert {base_handler1.mock.call_count, base_handler2.mock.call_count} == {2, 0}
        assert base_handler3.mock.call_count == 1
