import pytest

from faststream.redis import TestApp, TestRedisBroker


@pytest.mark.redis()
@pytest.mark.asyncio()
async def test_channel() -> None:
    from examples.redis.channel_sub import app, broker, handle

    async with TestRedisBroker(broker), TestApp(app):
        handle.mock.assert_called_once_with("Hi!")
