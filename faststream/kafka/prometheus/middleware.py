from collections.abc import Sequence
from typing import TYPE_CHECKING

from aiokafka import ConsumerRecord

from faststream._internal.constants import EMPTY
from faststream.kafka.prometheus.provider import settings_provider_factory
from faststream.kafka.response import KafkaPublishCommand
from faststream.prometheus.middleware import PrometheusMiddleware

if TYPE_CHECKING:
    from prometheus_client import CollectorRegistry


class KafkaPrometheusMiddleware(
    PrometheusMiddleware[
        KafkaPublishCommand,
        ConsumerRecord | Sequence[ConsumerRecord],
    ],
):
    def __init__(
        self,
        *,
        registry: "CollectorRegistry",
        app_name: str = EMPTY,
        metrics_prefix: str = "faststream",
        received_messages_size_buckets: Sequence[float] | None = None,
    ) -> None:
        super().__init__(
            settings_provider_factory=settings_provider_factory,  # type: ignore[arg-type]
            registry=registry,
            app_name=app_name,
            metrics_prefix=metrics_prefix,
            received_messages_size_buckets=received_messages_size_buckets,
        )
