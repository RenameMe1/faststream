import pytest

from tests.brokers.base.parser import CustomParserTestcase

from .basic import NatsTestcaseConfig


@pytest.mark.connected()
@pytest.mark.nats()
class TestCustomParser(NatsTestcaseConfig, CustomParserTestcase):
    pass
