import logging
from collections.abc import Callable, Iterable, Sequence
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    Optional,
    TypeVar,
    Union,
    cast,
    overload,
)

from aiokafka import ConsumerRecord
from aiokafka.coordinator.assignors.roundrobin import RoundRobinPartitionAssignor
from aiokafka.partitioner import DefaultPartitioner
from aiokafka.producer.producer import _missing
from fastapi.datastructures import Default
from fastapi.routing import APIRoute
from fastapi.utils import generate_unique_id
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute
from typing_extensions import Doc, deprecated, override

from faststream.__about__ import SERVICE_NAME
from faststream._internal.constants import EMPTY
from faststream._internal.context import ContextRepo
from faststream._internal.fastapi.router import StreamRouter
from faststream.kafka.broker.broker import KafkaBroker as KB
from faststream.middlewares import AckPolicy

if TYPE_CHECKING:
    from asyncio import AbstractEventLoop
    from enum import Enum

    from aiokafka import TopicPartition
    from aiokafka.abc import AbstractTokenProvider, ConsumerRebalanceListener
    from aiokafka.coordinator.assignors.abstract import AbstractPartitionAssignor
    from fastapi import params
    from fastapi.types import IncEx
    from starlette.types import ASGIApp, Lifespan

    from faststream._internal.basic_types import LoggerProto
    from faststream._internal.types import (
        BrokerMiddleware,
        CustomCallable,
        PublisherMiddleware,
        SubscriberMiddleware,
    )
    from faststream.kafka.publisher.usecase import (
        BatchPublisher,
        DefaultPublisher,
    )
    from faststream.kafka.subscriber.usecase import (
        BatchSubscriber,
        ConcurrentBetweenPartitionsSubscriber,
        ConcurrentDefaultSubscriber,
        DefaultSubscriber,
    )
    from faststream.security import BaseSecurity
    from faststream.specification.base import SpecificationFactory
    from faststream.specification.schema.extra import Tag, TagDict

Partition = TypeVar("Partition")


class KafkaRouter(StreamRouter[ConsumerRecord | tuple[ConsumerRecord, ...]]):
    """A class to represent a Kafka router."""

    broker_class = KB
    broker: KB

    def __init__(
        self,
        bootstrap_servers: Annotated[
            str | Iterable[str],
            Doc(
                """
            A `host[:port]` string (or list of `host[:port]` strings) that the consumer should contact to bootstrap
            initial cluster metadata.

            This does not have to be the full node list.
            It just needs to have at least one broker that will respond to a
            Metadata API Request. Default port is 9092.
            """,
            ),
        ] = "localhost",
        *,
        # both
        request_timeout_ms: Annotated[
            int,
            Doc("Client request timeout in milliseconds."),
        ] = 40 * 1000,
        retry_backoff_ms: Annotated[
            int,
            Doc("Milliseconds to backoff when retrying on errors."),
        ] = 100,
        metadata_max_age_ms: Annotated[
            int,
            Doc(
                """
            The period of time in milliseconds after
            which we force a refresh of metadata even if we haven't seen any
            partition leadership changes to proactively discover any new
            brokers or partitions.
            """,
            ),
        ] = 5 * 60 * 1000,
        connections_max_idle_ms: Annotated[
            int,
            Doc(
                """
             Close idle connections after the number
            of milliseconds specified by this config. Specifying `None` will
            disable idle checks.
            """,
            ),
        ] = 9 * 60 * 1000,
        sasl_kerberos_service_name: str = "kafka",
        sasl_kerberos_domain_name: str | None = None,
        sasl_oauth_token_provider: Annotated[
            Optional["AbstractTokenProvider"],
            Doc("OAuthBearer token provider instance."),
        ] = None,
        loop: Optional["AbstractEventLoop"] = None,
        client_id: Annotated[
            str | None,
            Doc(
                """
            A name for this client. This string is passed in
            each request to servers and can be used to identify specific
            server-side log entries that correspond to this client. Also
            submitted to :class:`~.consumer.group_coordinator.GroupCoordinator`
            for logging with respect to consumer group administration.
            """,
            ),
        ] = SERVICE_NAME,
        # publisher args
        acks: Annotated[
            Literal[0, 1, -1, "all"] | object,
            Doc(
                """
            One of ``0``, ``1``, ``all``. The number of acknowledgments
            the producer requires the leader to have received before considering a
            request complete. This controls the durability of records that are
            sent. The following settings are common:

            * ``0``: Producer will not wait for any acknowledgment from the server
              at all. The message will immediately be added to the socket
              buffer and considered sent. No guarantee can be made that the
              server has received the record in this case, and the retries
              configuration will not take effect (as the client won't
              generally know of any failures). The offset given back for each
              record will always be set to -1.
            * ``1``: The broker leader will write the record to its local log but
              will respond without awaiting full acknowledgement from all
              followers. In this case should the leader fail immediately
              after acknowledging the record but before the followers have
              replicated it then the record will be lost.
            * ``all``: The broker leader will wait for the full set of in-sync
              replicas to acknowledge the record. This guarantees that the
              record will not be lost as long as at least one in-sync replica
              remains alive. This is the strongest available guarantee.

            If unset, defaults to ``acks=1``. If `enable_idempotence` is
            :data:`True` defaults to ``acks=all``.
            """,
            ),
        ] = _missing,
        key_serializer: Annotated[
            Callable[[Any], bytes] | None,
            Doc("Used to convert user-supplied keys to bytes."),
        ] = None,
        value_serializer: Annotated[
            Callable[[Any], bytes] | None,
            Doc("used to convert user-supplied message values to bytes."),
        ] = None,
        compression_type: Annotated[
            Literal["gzip", "snappy", "lz4", "zstd"] | None,
            Doc(
                """
            The compression type for all data generated bythe producer.
            Compression is of full batches of data, so the efficacy of batching
            will also impact the compression ratio (more batching means better
            compression).
            """,
            ),
        ] = None,
        max_batch_size: Annotated[
            int,
            Doc(
                """
            Maximum size of buffered data per partition.
            After this amount `send` coroutine will block until batch is drained.
            """,
            ),
        ] = 16 * 1024,
        partitioner: Annotated[
            Callable[
                [bytes, list[Partition], list[Partition]],
                Partition,
            ],
            Doc(
                """
            Callable used to determine which partition
            each message is assigned to. Called (after key serialization):
            ``partitioner(key_bytes, all_partitions, available_partitions)``.
            The default partitioner implementation hashes each non-None key
            using the same murmur2 algorithm as the Java client so that
            messages with the same key are assigned to the same partition.
            When a key is :data:`None`, the message is delivered to a random partition
            (filtered to partitions with available leaders only, if possible).
            """,
            ),
        ] = DefaultPartitioner(),  # noqa: B008
        max_request_size: Annotated[
            int,
            Doc(
                """
            The maximum size of a request. This is also
            effectively a cap on the maximum record size. Note that the server
            has its own cap on record size which may be different from this.
            This setting will limit the number of record batches the producer
            will send in a single request to avoid sending huge requests.
            """,
            ),
        ] = 1024 * 1024,
        linger_ms: Annotated[
            int,
            Doc(
                """
            The producer groups together any records that arrive
            in between request transmissions into a single batched request.
            Normally this occurs only under load when records arrive faster
            than they can be sent out. However in some circumstances the client
            may want to reduce the number of requests even under moderate load.
            This setting accomplishes this by adding a small amount of
            artificial delay; that is, if first request is processed faster,
            than `linger_ms`, producer will wait ``linger_ms - process_time``.
            """,
            ),
        ] = 0,
        enable_idempotence: Annotated[
            bool,
            Doc(
                """
            When set to `True`, the producer will
            ensure that exactly one copy of each message is written in the
            stream. If `False`, producer retries due to broker failures,
            etc., may write duplicates of the retried message in the stream.
            Note that enabling idempotence acks to set to ``all``. If it is not
            explicitly set by the user it will be chosen.
            """,
            ),
        ] = False,
        transactional_id: str | None = None,
        transaction_timeout_ms: int = 60 * 1000,
        # broker base args
        graceful_timeout: Annotated[
            float | None,
            Doc(
                "Graceful shutdown timeout. Broker waits for all running subscribers completion before shut down.",
            ),
        ] = 15.0,
        decoder: Annotated[
            Optional["CustomCallable"],
            Doc("Custom decoder object."),
        ] = None,
        parser: Annotated[
            Optional["CustomCallable"],
            Doc("Custom parser object."),
        ] = None,
        middlewares: Annotated[
            Sequence["BrokerMiddleware[Any, Any]"],
            Doc("Middlewares to apply to all broker publishers/subscribers."),
        ] = (),
        # Specification args
        specification: Optional["SpecificationFactory"] = None,
        security: Annotated[
            Optional["BaseSecurity"],
            Doc(
                "Security options to connect broker and generate Specification server security information.",
            ),
        ] = None,
        specification_url: Annotated[
            str | None,
            Doc(
                "Specification hardcoded server addresses. Use `servers` if not specified.",
            ),
        ] = None,
        protocol: Annotated[
            str | None,
            Doc("Specification server protocol."),
        ] = None,
        protocol_version: Annotated[
            str | None,
            Doc("Specification server protocol version."),
        ] = "auto",
        description: Annotated[
            str | None,
            Doc("Specification server description."),
        ] = None,
        specification_tags: Annotated[
            Iterable[Union["Tag", "TagDict"]],
            Doc("Specification server tags."),
        ] = (),
        # logging args
        logger: Annotated[
            Optional["LoggerProto"],
            Doc("User specified logger to pass into Context and log service messages."),
        ] = EMPTY,
        log_level: Annotated[
            int,
            Doc("Service messages log level."),
        ] = logging.INFO,
        # StreamRouter options
        setup_state: Annotated[
            bool,
            Doc(
                "Whether to add broker to app scope in lifespan. "
                "You should disable this option at old ASGI servers.",
            ),
        ] = True,
        schema_url: Annotated[
            str | None,
            Doc(
                "Specification schema url. You should set this option to `None` to disable Specification routes at all.",
            ),
        ] = "/asyncapi",
        context: ContextRepo | None = None,
        # FastAPI args
        prefix: Annotated[
            str,
            Doc("An optional path prefix for the router."),
        ] = "",
        tags: Annotated[
            list[Union[str, "Enum"]] | None,
            Doc(
                """
                A list of tags to be applied to all the *path operations* in this
                router.

                It will be added to the generated OpenAPI (e.g. visible at `/docs`).

                Read more about it in the
                [FastAPI docs for Path Operation Configuration](https://fastapi.tiangolo.com/tutorial/path-operation-configuration/).
                """,
            ),
        ] = None,
        dependencies: Annotated[
            Sequence["params.Depends"] | None,
            Doc(
                """
                A list of dependencies (using `Depends()`) to be applied to all the
                *path and stream operations* in this router.

                Read more about it in the
                [FastAPI docs for Bigger Applications - Multiple Files](https://fastapi.tiangolo.com/tutorial/bigger-applications/#include-an-apirouter-with-a-custom-prefix-tags-responses-and-dependencies).
                """,
            ),
        ] = None,
        default_response_class: Annotated[
            type["Response"],
            Doc(
                """
                The default response class to be used.

                Read more in the
                [FastAPI docs for Custom Response - HTML, Stream, File, others](https://fastapi.tiangolo.com/advanced/custom-response/#default-response-class).
                """,
            ),
        ] = Default(JSONResponse),
        responses: Annotated[
            dict[int | str, dict[str, Any]] | None,
            Doc(
                """
                Additional responses to be shown in OpenAPI.

                It will be added to the generated OpenAPI (e.g. visible at `/docs`).

                Read more about it in the
                [FastAPI docs for Additional Responses in OpenAPI](https://fastapi.tiangolo.com/advanced/additional-responses/).

                And in the
                [FastAPI docs for Bigger Applications](https://fastapi.tiangolo.com/tutorial/bigger-applications/#include-an-apirouter-with-a-custom-prefix-tags-responses-and-dependencies).
                """,
            ),
        ] = None,
        callbacks: Annotated[
            list[BaseRoute] | None,
            Doc(
                """
                OpenAPI callbacks that should apply to all *path operations* in this
                router.

                It will be added to the generated OpenAPI (e.g. visible at `/docs`).

                Read more about it in the
                [FastAPI docs for OpenAPI Callbacks](https://fastapi.tiangolo.com/advanced/openapi-callbacks/).
                """,
            ),
        ] = None,
        routes: Annotated[
            list[BaseRoute] | None,
            Doc(
                """
                **Note**: you probably shouldn't use this parameter, it is inherited
                from Starlette and supported for compatibility.

                ---

                A list of routes to serve incoming HTTP and WebSocket requests.
                """,
            ),
            deprecated(
                """
                You normally wouldn't use this parameter with FastAPI, it is inherited
                from Starlette and supported for compatibility.

                In FastAPI, you normally would use the *path operation methods*,
                like `router.get()`, `router.post()`, etc.
                """,
            ),
        ] = None,
        redirect_slashes: Annotated[
            bool,
            Doc(
                """
                Whether to detect and redirect slashes in URLs when the client doesn't
                use the same format.
                """,
            ),
        ] = True,
        default: Annotated[
            Optional["ASGIApp"],
            Doc(
                """
                Default function handler for this router. Used to handle
                404 Not Found errors.
                """,
            ),
        ] = None,
        dependency_overrides_provider: Annotated[
            Any | None,
            Doc(
                """
                Only used internally by FastAPI to handle dependency overrides.

                You shouldn't need to use it. It normally points to the `FastAPI` app
                object.
                """,
            ),
        ] = None,
        route_class: Annotated[
            type["APIRoute"],
            Doc(
                """
                Custom route (*path operation*) class to be used by this router.

                Read more about it in the
                [FastAPI docs for Custom Request and APIRoute class](https://fastapi.tiangolo.com/how-to/custom-request-and-route/#custom-apiroute-class-in-a-router).
                """,
            ),
        ] = APIRoute,
        on_startup: Annotated[
            Sequence[Callable[[], Any]] | None,
            Doc(
                """
                A list of startup event handler functions.

                You should instead use the `lifespan` handlers.

                Read more in the [FastAPI docs for `lifespan`](https://fastapi.tiangolo.com/advanced/events/).
                """,
            ),
        ] = None,
        on_shutdown: Annotated[
            Sequence[Callable[[], Any]] | None,
            Doc(
                """
                A list of shutdown event handler functions.

                You should instead use the `lifespan` handlers.

                Read more in the
                [FastAPI docs for `lifespan`](https://fastapi.tiangolo.com/advanced/events/).
                """,
            ),
        ] = None,
        lifespan: Annotated[
            Optional["Lifespan[Any]"],
            Doc(
                """
                A `Lifespan` context manager handler. This replaces `startup` and
                `shutdown` functions with a single context manager.

                Read more in the
                [FastAPI docs for `lifespan`](https://fastapi.tiangolo.com/advanced/events/).
                """,
            ),
        ] = None,
        deprecated: Annotated[
            bool | None,
            Doc(
                """
                Mark all *path operations* in this router as deprecated.

                It will be added to the generated OpenAPI (e.g. visible at `/docs`).

                Read more about it in the
                [FastAPI docs for Path Operation Configuration](https://fastapi.tiangolo.com/tutorial/path-operation-configuration/).
                """,
            ),
        ] = None,
        include_in_schema: Annotated[
            bool,
            Doc(
                """
                To include (or not) all the *path operations* in this router in the
                generated OpenAPI.

                This affects the generated OpenAPI (e.g. visible at `/docs`).

                Read more about it in the
                [FastAPI docs for Query Parameters and String Validations](https://fastapi.tiangolo.com/tutorial/query-params-str-validations/#exclude-from-openapi).
                """,
            ),
        ] = True,
        generate_unique_id_function: Annotated[
            Callable[["APIRoute"], str],
            Doc(
                """
                Customize the function used to generate unique IDs for the *path
                operations* shown in the generated OpenAPI.

                This is particularly useful when automatically generating clients or
                SDKs for your API.

                Read more about it in the
                [FastAPI docs about how to Generate Clients](https://fastapi.tiangolo.com/advanced/generate-clients/#custom-generate-unique-id-function).
                """,
            ),
        ] = Default(generate_unique_id),
    ) -> None:
        super().__init__(
            bootstrap_servers=bootstrap_servers,
            # both args
            client_id=client_id,
            request_timeout_ms=request_timeout_ms,
            retry_backoff_ms=retry_backoff_ms,
            metadata_max_age_ms=metadata_max_age_ms,
            connections_max_idle_ms=connections_max_idle_ms,
            sasl_kerberos_service_name=sasl_kerberos_service_name,
            sasl_kerberos_domain_name=sasl_kerberos_domain_name,
            sasl_oauth_token_provider=sasl_oauth_token_provider,
            loop=loop,
            # publisher args
            acks=acks,
            key_serializer=key_serializer,
            value_serializer=value_serializer,
            compression_type=compression_type,
            max_batch_size=max_batch_size,
            partitioner=partitioner,
            max_request_size=max_request_size,
            linger_ms=linger_ms,
            enable_idempotence=enable_idempotence,
            transactional_id=transactional_id,
            transaction_timeout_ms=transaction_timeout_ms,
            # broker args
            graceful_timeout=graceful_timeout,
            decoder=decoder,
            parser=parser,
            middlewares=middlewares,
            schema_url=schema_url,
            setup_state=setup_state,
            context=context,
            # Logging args
            logger=logger,
            log_level=log_level,
            # Specification args
            security=security,
            protocol=protocol,
            description=description,
            protocol_version=protocol_version,
            specification_tags=specification_tags,
            specification_url=specification_url,
            # FastAPI args
            prefix=prefix,
            tags=tags,
            dependencies=dependencies,
            default_response_class=default_response_class,
            responses=responses,
            callbacks=callbacks,
            routes=routes,
            redirect_slashes=redirect_slashes,
            default=default,
            dependency_overrides_provider=dependency_overrides_provider,
            route_class=route_class,
            on_startup=on_startup,
            on_shutdown=on_shutdown,
            deprecated=deprecated,
            include_in_schema=include_in_schema,
            lifespan=lifespan,
            generate_unique_id_function=generate_unique_id_function,
            specification=specification,
        )

    @overload  # type: ignore[override]
    def subscriber(
        self,
        *topics: Annotated[str, Doc("Kafka topics to consume messages from.")],
        group_id: Annotated[
            str | None,
            Doc(
                """
            Name of the consumer group to join for dynamic
            partition assignment (if enabled), and to use for fetching and
            committing offsets. If `None`, auto-partition assignment (via
            group coordinator) and offset commits are disabled.
            """,
            ),
        ] = None,
        key_deserializer: Annotated[
            Callable[[bytes], Any] | None,
            Doc(
                "Any callable that takes a raw message `bytes` "
                "key and returns a deserialized one.",
            ),
        ] = None,
        value_deserializer: Annotated[
            Callable[[bytes], Any] | None,
            Doc(
                "Any callable that takes a raw message `bytes` "
                "value and returns a deserialized value.",
            ),
        ] = None,
        fetch_max_bytes: Annotated[
            int,
            Doc(
                """
            The maximum amount of data the server should
            return for a fetch request. This is not an absolute maximum, if
            the first message in the first non-empty partition of the fetch
            is larger than this value, the message will still be returned
            to ensure that the consumer can make progress. NOTE: consumer
            performs fetches to multiple brokers in parallel so memory
            usage will depend on the number of brokers containing
            partitions for the topic.
            """,
            ),
        ] = 50 * 1024 * 1024,
        fetch_min_bytes: Annotated[
            int,
            Doc(
                """
            Minimum amount of data the server should
            return for a fetch request, otherwise wait up to
            `fetch_max_wait_ms` for more data to accumulate.
            """,
            ),
        ] = 1,
        fetch_max_wait_ms: Annotated[
            int,
            Doc(
                """
            The maximum amount of time in milliseconds
            the server will block before answering the fetch request if
            there isn't sufficient data to immediately satisfy the
            requirement given by `fetch_min_bytes`.
            """,
            ),
        ] = 500,
        max_partition_fetch_bytes: Annotated[
            int,
            Doc(
                """
            The maximum amount of data
            per-partition the server will return. The maximum total memory
            used for a request ``= #partitions * max_partition_fetch_bytes``.
            This size must be at least as large as the maximum message size
            the server allows or else it is possible for the producer to
            send messages larger than the consumer can fetch. If that
            happens, the consumer can get stuck trying to fetch a large
            message on a certain partition.
            """,
            ),
        ] = 1 * 1024 * 1024,
        auto_offset_reset: Annotated[
            Literal["latest", "earliest", "none"],
            Doc(
                """
            A policy for resetting offsets on `OffsetOutOfRangeError` errors:

            * `earliest` will move to the oldest available message
            * `latest` will move to the most recent
            * `none` will raise an exception so you can handle this case
            """,
            ),
        ] = "latest",
        auto_commit: Annotated[
            bool,
            Doc(
                """
            If `True` the consumer's offset will be
            periodically committed in the background.
            """,
            ),
            deprecated(
                "This option is deprecated and will be removed in 0.7.0 release. "
                "Please, use `ack_policy=AckPolicy.ACK_FIRST` instead."
            ),
        ] = EMPTY,
        auto_commit_interval_ms: Annotated[
            int,
            Doc(
                """
            Milliseconds between automatic
            offset commits, if `auto_commit` is `True`.""",
            ),
        ] = 5 * 1000,
        check_crcs: Annotated[
            bool,
            Doc(
                """
            Automatically check the CRC32 of the records
            consumed. This ensures no on-the-wire or on-disk corruption to
            the messages occurred. This check adds some overhead, so it may
            be disabled in cases seeking extreme performance.
            """,
            ),
        ] = True,
        partition_assignment_strategy: Annotated[
            Sequence["AbstractPartitionAssignor"],
            Doc(
                """
            List of objects to use to
            distribute partition ownership amongst consumer instances when
            group management is used. This preference is implicit in the order
            of the strategies in the list. When assignment strategy changes:
            to support a change to the assignment strategy, new versions must
            enable support both for the old assignment strategy and the new
            one. The coordinator will choose the old assignment strategy until
            all members have been updated. Then it will choose the new
            strategy.
            """,
            ),
        ] = (RoundRobinPartitionAssignor,),
        max_poll_interval_ms: Annotated[
            int,
            Doc(
                """
            Maximum allowed time between calls to
            consume messages in batches. If this interval
            is exceeded the consumer is considered failed and the group will
            rebalance in order to reassign the partitions to another consumer
            group member. If API methods block waiting for messages, that time
            does not count against this timeout.
            """,
            ),
        ] = 5 * 60 * 1000,
        rebalance_timeout_ms: Annotated[
            int | None,
            Doc(
                """
            The maximum time server will wait for this
            consumer to rejoin the group in a case of rebalance. In Java client
            this behaviour is bound to `max.poll.interval.ms` configuration,
            but as ``aiokafka`` will rejoin the group in the background, we
            decouple this setting to allow finer tuning by users that use
            `ConsumerRebalanceListener` to delay rebalacing. Defaults
            to ``session_timeout_ms``
            """,
            ),
        ] = None,
        session_timeout_ms: Annotated[
            int,
            Doc(
                """
            Client group session and failure detection
            timeout. The consumer sends periodic heartbeats
            (`heartbeat.interval.ms`) to indicate its liveness to the broker.
            If no hearts are received by the broker for a group member within
            the session timeout, the broker will remove the consumer from the
            group and trigger a rebalance. The allowed range is configured with
            the **broker** configuration properties
            `group.min.session.timeout.ms` and `group.max.session.timeout.ms`.
            """,
            ),
        ] = 10 * 1000,
        heartbeat_interval_ms: Annotated[
            int,
            Doc(
                """
            The expected time in milliseconds
            between heartbeats to the consumer coordinator when using
            Kafka's group management feature. Heartbeats are used to ensure
            that the consumer's session stays active and to facilitate
            rebalancing when new consumers join or leave the group. The
            value must be set lower than `session_timeout_ms`, but typically
            should be set no higher than 1/3 of that value. It can be
            adjusted even lower to control the expected time for normal
            rebalances.
            """,
            ),
        ] = 3 * 1000,
        consumer_timeout_ms: Annotated[
            int,
            Doc(
                """
            Maximum wait timeout for background fetching
            routine. Mostly defines how fast the system will see rebalance and
            request new data for new partitions.
            """,
            ),
        ] = 200,
        max_poll_records: Annotated[
            int | None,
            Doc(
                """
            The maximum number of records returned in a
            single call by batch consumer. Has no limit by default.
            """,
            ),
        ] = None,
        exclude_internal_topics: Annotated[
            bool,
            Doc(
                """
            Whether records from internal topics
            (such as offsets) should be exposed to the consumer. If set to True
            the only way to receive records from an internal topic is
            subscribing to it.
            """,
            ),
        ] = True,
        isolation_level: Annotated[
            Literal["read_uncommitted", "read_committed"],
            Doc(
                """
            Controls how to read messages written
            transactionally.

            * `read_committed`, batch consumer will only return
            transactional messages which have been committed.

            * `read_uncommitted` (the default), batch consumer will
            return all messages, even transactional messages which have been
            aborted.

            Non-transactional messages will be returned unconditionally in
            either mode.

            Messages will always be returned in offset order. Hence, in
            `read_committed` mode, batch consumer will only return
            messages up to the last stable offset (ALSO), which is the one less
            than the offset of the first open transaction. In particular any
            messages appearing after messages belonging to ongoing transactions
            will be withheld until the relevant transaction has been completed.
            As a result, `read_committed` consumers will not be able to read up
            to the high watermark when there are in flight transactions.
            Further, when in `read_committed` the seek_to_end method will
            return the ALSO. See method docs below.
            """,
            ),
        ] = "read_uncommitted",
        batch_timeout_ms: Annotated[
            int,
            Doc(
                """
            Milliseconds spent waiting if
            data is not available in the buffer. If 0, returns immediately
            with any records that are available currently in the buffer,
            else returns empty.
            """,
            ),
        ] = 200,
        max_records: Annotated[
            int | None,
            Doc("Number of messages to consume as one batch."),
        ] = None,
        batch: Annotated[
            Literal[False],
            Doc("Whether to consume messages in batches or not."),
        ] = False,
        listener: Annotated[
            Optional["ConsumerRebalanceListener"],
            Doc(
                """
            Optionally include listener
               callback, which will be called before and after each rebalance
               operation.
               As part of group management, the consumer will keep track of
               the list of consumers that belong to a particular group and
               will trigger a rebalance operation if one of the following
               events trigger:

               * Number of partitions change for any of the subscribed topics
               * Topic is created or deleted
               * An existing member of the consumer group dies
               * A new member is added to the consumer group

               When any of these events are triggered, the provided listener
               will be invoked first to indicate that the consumer's
               assignment has been revoked, and then again when the new
               assignment has been received. Note that this listener will
               immediately override any listener set in a previous call
               to subscribe. It is guaranteed, however, that the partitions
               revoked/assigned
               through this interface are from topics subscribed in this call.
            """,
            ),
        ] = None,
        pattern: Annotated[
            str | None,
            Doc(
                """
            Pattern to match available topics. You must provide either topics or pattern, but not both.
            """,
            ),
        ] = None,
        partitions: Annotated[
            Iterable["TopicPartition"],
            Doc(
                """
            An explicit partitions list to assign.
            You can't use 'topics' and 'partitions' in the same time.
            """,
            ),
        ] = (),
        # broker args
        dependencies: Annotated[
            Iterable["params.Depends"],
            Doc("Dependencies list (`[Depends(),]`) to apply to the subscriber."),
        ] = (),
        parser: Annotated[
            Optional["CustomCallable"],
            Doc("Parser to map original **ConsumerRecord** object to FastStream one."),
        ] = None,
        decoder: Annotated[
            Optional["CustomCallable"],
            Doc("Function to decode FastStream msg bytes body to python objects."),
        ] = None,
        middlewares: Annotated[
            Sequence["SubscriberMiddleware[Any]"],
            deprecated(
                "This option was deprecated in 0.6.0. Use router-level middlewares instead."
                "Scheduled to remove in 0.7.0",
            ),
            Doc("Subscriber middlewares to wrap incoming message processing."),
        ] = (),
        no_ack: Annotated[
            bool,
            Doc("Whether to disable **FastStream** auto acknowledgement logic or not."),
            deprecated(
                "This option was deprecated in 0.6.0 to prior to **ack_policy=AckPolicy.MANUAL**. "
                "Scheduled to remove in 0.7.0",
            ),
        ] = EMPTY,
        ack_policy: AckPolicy = EMPTY,
        no_reply: Annotated[
            bool,
            Doc(
                "Whether to disable **FastStream** RPC and Reply To auto responses or not.",
            ),
        ] = False,
        # Specification information
        title: Annotated[
            str | None,
            Doc("Specification subscriber object title."),
        ] = None,
        description: Annotated[
            str | None,
            Doc(
                "Specification subscriber object description. "
                "Uses decorated docstring as default.",
            ),
        ] = None,
        include_in_schema: Annotated[
            bool,
            Doc("Whetever to include operation in Specification schema or not."),
        ] = True,
        # FastAPI args
        response_model: Annotated[
            Any,
            Doc(
                """
                The type to use for the response.

                It could be any valid Pydantic *field* type. So, it doesn't have to
                be a Pydantic model, it could be other things, like a `list`, `dict`,
                etc.

                It will be used for:

                * Documentation: the generated OpenAPI (and the UI at `/docs`) will
                    show it as the response (JSON Schema).
                * Serialization: you could return an arbitrary object and the
                    `response_model` would be used to serialize that object into the
                    corresponding JSON.
                * Filtering: the JSON sent to the client will only contain the data
                    (fields) defined in the `response_model`. If you returned an object
                    that contains an attribute `password` but the `response_model` does
                    not include that field, the JSON sent to the client would not have
                    that `password`.
                * Validation: whatever you return will be serialized with the
                    `response_model`, converting any data as necessary to generate the
                    corresponding JSON. But if the data in the object returned is not
                    valid, that would mean a violation of the contract with the client,
                    so it's an error from the API developer. So, FastAPI will raise an
                    error and return a 500 error code (Internal Server Error).

                Read more about it in the
                [FastAPI docs for Response Model](https://fastapi.tiangolo.com/tutorial/response-model/).
                """,
            ),
        ] = Default(None),
        response_model_include: Annotated[
            Optional["IncEx"],
            Doc(
                """
                Configuration passed to Pydantic to include only certain fields in the
                response data.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#response_model_include-and-response_model_exclude).
                """,
            ),
        ] = None,
        response_model_exclude: Annotated[
            Optional["IncEx"],
            Doc(
                """
                Configuration passed to Pydantic to exclude certain fields in the
                response data.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#response_model_include-and-response_model_exclude).
                """,
            ),
        ] = None,
        response_model_by_alias: Annotated[
            bool,
            Doc(
                """
                Configuration passed to Pydantic to define if the response model
                should be serialized by alias when an alias is used.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#response_model_include-and-response_model_exclude).
                """,
            ),
        ] = True,
        response_model_exclude_unset: Annotated[
            bool,
            Doc(
                """
                Configuration passed to Pydantic to define if the response data
                should have all the fields, including the ones that were not set and
                have their default values. This is different from
                `response_model_exclude_defaults` in that if the fields are set,
                they will be included in the response, even if the value is the same
                as the default.

                When `True`, default values are omitted from the response.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#use-the-response_model_exclude_unset-parameter).
                """,
            ),
        ] = False,
        response_model_exclude_defaults: Annotated[
            bool,
            Doc(
                """
                Configuration passed to Pydantic to define if the response data
                should have all the fields, including the ones that have the same value
                as the default. This is different from `response_model_exclude_unset`
                in that if the fields are set but contain the same default values,
                they will be excluded from the response.

                When `True`, default values are omitted from the response.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#use-the-response_model_exclude_unset-parameter).
                """,
            ),
        ] = False,
        response_model_exclude_none: Annotated[
            bool,
            Doc(
                """
                Configuration passed to Pydantic to define if the response data should
                exclude fields set to `None`.

                This is much simpler (less smart) than `response_model_exclude_unset`
                and `response_model_exclude_defaults`. You probably want to use one of
                those two instead of this one, as those allow returning `None` values
                when it makes sense.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#response_model_exclude_none).
                """,
            ),
        ] = False,
    ) -> "DefaultSubscriber": ...

    @overload
    def subscriber(
        self,
        *topics: Annotated[str, Doc("Kafka topics to consume messages from.")],
        group_id: Annotated[
            str | None,
            Doc(
                """
            Name of the consumer group to join for dynamic
            partition assignment (if enabled), and to use for fetching and
            committing offsets. If `None`, auto-partition assignment (via
            group coordinator) and offset commits are disabled.
            """,
            ),
        ] = None,
        key_deserializer: Annotated[
            Callable[[bytes], Any] | None,
            Doc(
                "Any callable that takes a raw message `bytes` "
                "key and returns a deserialized one.",
            ),
        ] = None,
        value_deserializer: Annotated[
            Callable[[bytes], Any] | None,
            Doc(
                "Any callable that takes a raw message `bytes` "
                "value and returns a deserialized value.",
            ),
        ] = None,
        fetch_max_bytes: Annotated[
            int,
            Doc(
                """
            The maximum amount of data the server should
            return for a fetch request. This is not an absolute maximum, if
            the first message in the first non-empty partition of the fetch
            is larger than this value, the message will still be returned
            to ensure that the consumer can make progress. NOTE: consumer
            performs fetches to multiple brokers in parallel so memory
            usage will depend on the number of brokers containing
            partitions for the topic.
            """,
            ),
        ] = 50 * 1024 * 1024,
        fetch_min_bytes: Annotated[
            int,
            Doc(
                """
            Minimum amount of data the server should
            return for a fetch request, otherwise wait up to
            `fetch_max_wait_ms` for more data to accumulate.
            """,
            ),
        ] = 1,
        fetch_max_wait_ms: Annotated[
            int,
            Doc(
                """
            The maximum amount of time in milliseconds
            the server will block before answering the fetch request if
            there isn't sufficient data to immediately satisfy the
            requirement given by `fetch_min_bytes`.
            """,
            ),
        ] = 500,
        max_partition_fetch_bytes: Annotated[
            int,
            Doc(
                """
            The maximum amount of data
            per-partition the server will return. The maximum total memory
            used for a request ``= #partitions * max_partition_fetch_bytes``.
            This size must be at least as large as the maximum message size
            the server allows or else it is possible for the producer to
            send messages larger than the consumer can fetch. If that
            happens, the consumer can get stuck trying to fetch a large
            message on a certain partition.
            """,
            ),
        ] = 1 * 1024 * 1024,
        auto_offset_reset: Annotated[
            Literal["latest", "earliest", "none"],
            Doc(
                """
            A policy for resetting offsets on `OffsetOutOfRangeError` errors:

            * `earliest` will move to the oldest available message
            * `latest` will move to the most recent
            * `none` will raise an exception so you can handle this case
            """,
            ),
        ] = "latest",
        auto_commit: Annotated[
            bool,
            Doc(
                """
            If `True` the consumer's offset will be
            periodically committed in the background.
            """,
            ),
            deprecated(
                "This option is deprecated and will be removed in 0.7.0 release. "
                "Please, use `ack_policy=AckPolicy.ACK_FIRST` instead."
            ),
        ] = EMPTY,
        auto_commit_interval_ms: Annotated[
            int,
            Doc(
                """
            Milliseconds between automatic
            offset commits, if `auto_commit` is `True`.""",
            ),
        ] = 5 * 1000,
        check_crcs: Annotated[
            bool,
            Doc(
                """
            Automatically check the CRC32 of the records
            consumed. This ensures no on-the-wire or on-disk corruption to
            the messages occurred. This check adds some overhead, so it may
            be disabled in cases seeking extreme performance.
            """,
            ),
        ] = True,
        partition_assignment_strategy: Annotated[
            Sequence["AbstractPartitionAssignor"],
            Doc(
                """
            List of objects to use to
            distribute partition ownership amongst consumer instances when
            group management is used. This preference is implicit in the order
            of the strategies in the list. When assignment strategy changes:
            to support a change to the assignment strategy, new versions must
            enable support both for the old assignment strategy and the new
            one. The coordinator will choose the old assignment strategy until
            all members have been updated. Then it will choose the new
            strategy.
            """,
            ),
        ] = (RoundRobinPartitionAssignor,),
        max_poll_interval_ms: Annotated[
            int,
            Doc(
                """
            Maximum allowed time between calls to
            consume messages in batches. If this interval
            is exceeded the consumer is considered failed and the group will
            rebalance in order to reassign the partitions to another consumer
            group member. If API methods block waiting for messages, that time
            does not count against this timeout.
            """,
            ),
        ] = 5 * 60 * 1000,
        rebalance_timeout_ms: Annotated[
            int | None,
            Doc(
                """
            The maximum time server will wait for this
            consumer to rejoin the group in a case of rebalance. In Java client
            this behaviour is bound to `max.poll.interval.ms` configuration,
            but as ``aiokafka`` will rejoin the group in the background, we
            decouple this setting to allow finer tuning by users that use
            `ConsumerRebalanceListener` to delay rebalacing. Defaults
            to ``session_timeout_ms``
            """,
            ),
        ] = None,
        session_timeout_ms: Annotated[
            int,
            Doc(
                """
            Client group session and failure detection
            timeout. The consumer sends periodic heartbeats
            (`heartbeat.interval.ms`) to indicate its liveness to the broker.
            If no hearts are received by the broker for a group member within
            the session timeout, the broker will remove the consumer from the
            group and trigger a rebalance. The allowed range is configured with
            the **broker** configuration properties
            `group.min.session.timeout.ms` and `group.max.session.timeout.ms`.
            """,
            ),
        ] = 10 * 1000,
        heartbeat_interval_ms: Annotated[
            int,
            Doc(
                """
            The expected time in milliseconds
            between heartbeats to the consumer coordinator when using
            Kafka's group management feature. Heartbeats are used to ensure
            that the consumer's session stays active and to facilitate
            rebalancing when new consumers join or leave the group. The
            value must be set lower than `session_timeout_ms`, but typically
            should be set no higher than 1/3 of that value. It can be
            adjusted even lower to control the expected time for normal
            rebalances.
            """,
            ),
        ] = 3 * 1000,
        consumer_timeout_ms: Annotated[
            int,
            Doc(
                """
            Maximum wait timeout for background fetching
            routine. Mostly defines how fast the system will see rebalance and
            request new data for new partitions.
            """,
            ),
        ] = 200,
        max_poll_records: Annotated[
            int | None,
            Doc(
                """
            The maximum number of records returned in a
            single call by batch consumer. Has no limit by default.
            """,
            ),
        ] = None,
        exclude_internal_topics: Annotated[
            bool,
            Doc(
                """
            Whether records from internal topics
            (such as offsets) should be exposed to the consumer. If set to True
            the only way to receive records from an internal topic is
            subscribing to it.
            """,
            ),
        ] = True,
        isolation_level: Annotated[
            Literal["read_uncommitted", "read_committed"],
            Doc(
                """
            Controls how to read messages written
            transactionally.

            * `read_committed`, batch consumer will only return
            transactional messages which have been committed.

            * `read_uncommitted` (the default), batch consumer will
            return all messages, even transactional messages which have been
            aborted.

            Non-transactional messages will be returned unconditionally in
            either mode.

            Messages will always be returned in offset order. Hence, in
            `read_committed` mode, batch consumer will only return
            messages up to the last stable offset (ALSO), which is the one less
            than the offset of the first open transaction. In particular any
            messages appearing after messages belonging to ongoing transactions
            will be withheld until the relevant transaction has been completed.
            As a result, `read_committed` consumers will not be able to read up
            to the high watermark when there are in flight transactions.
            Further, when in `read_committed` the seek_to_end method will
            return the ALSO. See method docs below.
            """,
            ),
        ] = "read_uncommitted",
        batch_timeout_ms: Annotated[
            int,
            Doc(
                """
            Milliseconds spent waiting if
            data is not available in the buffer. If 0, returns immediately
            with any records that are available currently in the buffer,
            else returns empty.
            """,
            ),
        ] = 200,
        max_records: Annotated[
            int | None,
            Doc("Number of messages to consume as one batch."),
        ] = None,
        batch: Annotated[
            Literal[True],
            Doc("Whether to consume messages in batches or not."),
        ],
        listener: Annotated[
            Optional["ConsumerRebalanceListener"],
            Doc(
                """
            Optionally include listener
               callback, which will be called before and after each rebalance
               operation.
               As part of group management, the consumer will keep track of
               the list of consumers that belong to a particular group and
               will trigger a rebalance operation if one of the following
               events trigger:

               * Number of partitions change for any of the subscribed topics
               * Topic is created or deleted
               * An existing member of the consumer group dies
               * A new member is added to the consumer group

               When any of these events are triggered, the provided listener
               will be invoked first to indicate that the consumer's
               assignment has been revoked, and then again when the new
               assignment has been received. Note that this listener will
               immediately override any listener set in a previous call
               to subscribe. It is guaranteed, however, that the partitions
               revoked/assigned
               through this interface are from topics subscribed in this call.
            """,
            ),
        ] = None,
        pattern: Annotated[
            str | None,
            Doc(
                """
            Pattern to match available topics. You must provide either topics or pattern, but not both.
            """,
            ),
        ] = None,
        partitions: Annotated[
            Iterable["TopicPartition"],
            Doc(
                """
            An explicit partitions list to assign.
            You can't use 'topics' and 'partitions' in the same time.
            """,
            ),
        ] = (),
        # broker args
        dependencies: Annotated[
            Iterable["params.Depends"],
            Doc("Dependencies list (`[Depends(),]`) to apply to the subscriber."),
        ] = (),
        parser: Annotated[
            Optional["CustomCallable"],
            Doc("Parser to map original **ConsumerRecord** object to FastStream one."),
        ] = None,
        decoder: Annotated[
            Optional["CustomCallable"],
            Doc("Function to decode FastStream msg bytes body to python objects."),
        ] = None,
        middlewares: Annotated[
            Sequence["SubscriberMiddleware[Any]"],
            deprecated(
                "This option was deprecated in 0.6.0. Use router-level middlewares instead."
                "Scheduled to remove in 0.7.0",
            ),
            Doc("Subscriber middlewares to wrap incoming message processing."),
        ] = (),
        no_ack: Annotated[
            bool,
            Doc("Whether to disable **FastStream** auto acknowledgement logic or not."),
            deprecated(
                "This option was deprecated in 0.6.0 to prior to **ack_policy=AckPolicy.MANUAL**. "
                "Scheduled to remove in 0.7.0",
            ),
        ] = EMPTY,
        ack_policy: AckPolicy = EMPTY,
        no_reply: Annotated[
            bool,
            Doc(
                "Whether to disable **FastStream** RPC and Reply To auto responses or not.",
            ),
        ] = False,
        # Specification information
        title: Annotated[
            str | None,
            Doc("Specification subscriber object title."),
        ] = None,
        description: Annotated[
            str | None,
            Doc(
                "Specification subscriber object description. "
                "Uses decorated docstring as default.",
            ),
        ] = None,
        include_in_schema: Annotated[
            bool,
            Doc("Whetever to include operation in Specification schema or not."),
        ] = True,
        # FastAPI args
        response_model: Annotated[
            Any,
            Doc(
                """
                The type to use for the response.

                It could be any valid Pydantic *field* type. So, it doesn't have to
                be a Pydantic model, it could be other things, like a `list`, `dict`,
                etc.

                It will be used for:

                * Documentation: the generated OpenAPI (and the UI at `/docs`) will
                    show it as the response (JSON Schema).
                * Serialization: you could return an arbitrary object and the
                    `response_model` would be used to serialize that object into the
                    corresponding JSON.
                * Filtering: the JSON sent to the client will only contain the data
                    (fields) defined in the `response_model`. If you returned an object
                    that contains an attribute `password` but the `response_model` does
                    not include that field, the JSON sent to the client would not have
                    that `password`.
                * Validation: whatever you return will be serialized with the
                    `response_model`, converting any data as necessary to generate the
                    corresponding JSON. But if the data in the object returned is not
                    valid, that would mean a violation of the contract with the client,
                    so it's an error from the API developer. So, FastAPI will raise an
                    error and return a 500 error code (Internal Server Error).

                Read more about it in the
                [FastAPI docs for Response Model](https://fastapi.tiangolo.com/tutorial/response-model/).
                """,
            ),
        ] = Default(None),
        response_model_include: Annotated[
            Optional["IncEx"],
            Doc(
                """
                Configuration passed to Pydantic to include only certain fields in the
                response data.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#response_model_include-and-response_model_exclude).
                """,
            ),
        ] = None,
        response_model_exclude: Annotated[
            Optional["IncEx"],
            Doc(
                """
                Configuration passed to Pydantic to exclude certain fields in the
                response data.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#response_model_include-and-response_model_exclude).
                """,
            ),
        ] = None,
        response_model_by_alias: Annotated[
            bool,
            Doc(
                """
                Configuration passed to Pydantic to define if the response model
                should be serialized by alias when an alias is used.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#response_model_include-and-response_model_exclude).
                """,
            ),
        ] = True,
        response_model_exclude_unset: Annotated[
            bool,
            Doc(
                """
                Configuration passed to Pydantic to define if the response data
                should have all the fields, including the ones that were not set and
                have their default values. This is different from
                `response_model_exclude_defaults` in that if the fields are set,
                they will be included in the response, even if the value is the same
                as the default.

                When `True`, default values are omitted from the response.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#use-the-response_model_exclude_unset-parameter).
                """,
            ),
        ] = False,
        response_model_exclude_defaults: Annotated[
            bool,
            Doc(
                """
                Configuration passed to Pydantic to define if the response data
                should have all the fields, including the ones that have the same value
                as the default. This is different from `response_model_exclude_unset`
                in that if the fields are set but contain the same default values,
                they will be excluded from the response.

                When `True`, default values are omitted from the response.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#use-the-response_model_exclude_unset-parameter).
                """,
            ),
        ] = False,
        response_model_exclude_none: Annotated[
            bool,
            Doc(
                """
                Configuration passed to Pydantic to define if the response data should
                exclude fields set to `None`.

                This is much simpler (less smart) than `response_model_exclude_unset`
                and `response_model_exclude_defaults`. You probably want to use one of
                those two instead of this one, as those allow returning `None` values
                when it makes sense.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#response_model_exclude_none).
                """,
            ),
        ] = False,
    ) -> "BatchSubscriber": ...

    @overload
    def subscriber(
        self,
        *topics: Annotated[str, Doc("Kafka topics to consume messages from.")],
        group_id: Annotated[
            str | None,
            Doc(
                """
            Name of the consumer group to join for dynamic
            partition assignment (if enabled), and to use for fetching and
            committing offsets. If `None`, auto-partition assignment (via
            group coordinator) and offset commits are disabled.
            """,
            ),
        ] = None,
        key_deserializer: Annotated[
            Callable[[bytes], Any] | None,
            Doc(
                "Any callable that takes a raw message `bytes` "
                "key and returns a deserialized one.",
            ),
        ] = None,
        value_deserializer: Annotated[
            Callable[[bytes], Any] | None,
            Doc(
                "Any callable that takes a raw message `bytes` "
                "value and returns a deserialized value.",
            ),
        ] = None,
        fetch_max_bytes: Annotated[
            int,
            Doc(
                """
            The maximum amount of data the server should
            return for a fetch request. This is not an absolute maximum, if
            the first message in the first non-empty partition of the fetch
            is larger than this value, the message will still be returned
            to ensure that the consumer can make progress. NOTE: consumer
            performs fetches to multiple brokers in parallel so memory
            usage will depend on the number of brokers containing
            partitions for the topic.
            """,
            ),
        ] = 50 * 1024 * 1024,
        fetch_min_bytes: Annotated[
            int,
            Doc(
                """
            Minimum amount of data the server should
            return for a fetch request, otherwise wait up to
            `fetch_max_wait_ms` for more data to accumulate.
            """,
            ),
        ] = 1,
        fetch_max_wait_ms: Annotated[
            int,
            Doc(
                """
            The maximum amount of time in milliseconds
            the server will block before answering the fetch request if
            there isn't sufficient data to immediately satisfy the
            requirement given by `fetch_min_bytes`.
            """,
            ),
        ] = 500,
        max_partition_fetch_bytes: Annotated[
            int,
            Doc(
                """
            The maximum amount of data
            per-partition the server will return. The maximum total memory
            used for a request ``= #partitions * max_partition_fetch_bytes``.
            This size must be at least as large as the maximum message size
            the server allows or else it is possible for the producer to
            send messages larger than the consumer can fetch. If that
            happens, the consumer can get stuck trying to fetch a large
            message on a certain partition.
            """,
            ),
        ] = 1 * 1024 * 1024,
        auto_offset_reset: Annotated[
            Literal["latest", "earliest", "none"],
            Doc(
                """
            A policy for resetting offsets on `OffsetOutOfRangeError` errors:

            * `earliest` will move to the oldest available message
            * `latest` will move to the most recent
            * `none` will raise an exception so you can handle this case
            """,
            ),
        ] = "latest",
        auto_commit: Annotated[
            bool,
            Doc(
                """
            If `True` the consumer's offset will be
            periodically committed in the background.
            """,
            ),
            deprecated(
                "This option is deprecated and will be removed in 0.7.0 release. "
                "Please, use `ack_policy=AckPolicy.ACK_FIRST` instead."
            ),
        ] = EMPTY,
        auto_commit_interval_ms: Annotated[
            int,
            Doc(
                """
            Milliseconds between automatic
            offset commits, if `auto_commit` is `True`.""",
            ),
        ] = 5 * 1000,
        check_crcs: Annotated[
            bool,
            Doc(
                """
            Automatically check the CRC32 of the records
            consumed. This ensures no on-the-wire or on-disk corruption to
            the messages occurred. This check adds some overhead, so it may
            be disabled in cases seeking extreme performance.
            """,
            ),
        ] = True,
        partition_assignment_strategy: Annotated[
            Sequence["AbstractPartitionAssignor"],
            Doc(
                """
            List of objects to use to
            distribute partition ownership amongst consumer instances when
            group management is used. This preference is implicit in the order
            of the strategies in the list. When assignment strategy changes:
            to support a change to the assignment strategy, new versions must
            enable support both for the old assignment strategy and the new
            one. The coordinator will choose the old assignment strategy until
            all members have been updated. Then it will choose the new
            strategy.
            """,
            ),
        ] = (RoundRobinPartitionAssignor,),
        max_poll_interval_ms: Annotated[
            int,
            Doc(
                """
            Maximum allowed time between calls to
            consume messages in batches. If this interval
            is exceeded the consumer is considered failed and the group will
            rebalance in order to reassign the partitions to another consumer
            group member. If API methods block waiting for messages, that time
            does not count against this timeout.
            """,
            ),
        ] = 5 * 60 * 1000,
        rebalance_timeout_ms: Annotated[
            int | None,
            Doc(
                """
            The maximum time server will wait for this
            consumer to rejoin the group in a case of rebalance. In Java client
            this behaviour is bound to `max.poll.interval.ms` configuration,
            but as ``aiokafka`` will rejoin the group in the background, we
            decouple this setting to allow finer tuning by users that use
            `ConsumerRebalanceListener` to delay rebalacing. Defaults
            to ``session_timeout_ms``
            """,
            ),
        ] = None,
        session_timeout_ms: Annotated[
            int,
            Doc(
                """
            Client group session and failure detection
            timeout. The consumer sends periodic heartbeats
            (`heartbeat.interval.ms`) to indicate its liveness to the broker.
            If no hearts are received by the broker for a group member within
            the session timeout, the broker will remove the consumer from the
            group and trigger a rebalance. The allowed range is configured with
            the **broker** configuration properties
            `group.min.session.timeout.ms` and `group.max.session.timeout.ms`.
            """,
            ),
        ] = 10 * 1000,
        heartbeat_interval_ms: Annotated[
            int,
            Doc(
                """
            The expected time in milliseconds
            between heartbeats to the consumer coordinator when using
            Kafka's group management feature. Heartbeats are used to ensure
            that the consumer's session stays active and to facilitate
            rebalancing when new consumers join or leave the group. The
            value must be set lower than `session_timeout_ms`, but typically
            should be set no higher than 1/3 of that value. It can be
            adjusted even lower to control the expected time for normal
            rebalances.
            """,
            ),
        ] = 3 * 1000,
        consumer_timeout_ms: Annotated[
            int,
            Doc(
                """
            Maximum wait timeout for background fetching
            routine. Mostly defines how fast the system will see rebalance and
            request new data for new partitions.
            """,
            ),
        ] = 200,
        max_poll_records: Annotated[
            int | None,
            Doc(
                """
            The maximum number of records returned in a
            single call by batch consumer. Has no limit by default.
            """,
            ),
        ] = None,
        exclude_internal_topics: Annotated[
            bool,
            Doc(
                """
            Whether records from internal topics
            (such as offsets) should be exposed to the consumer. If set to True
            the only way to receive records from an internal topic is
            subscribing to it.
            """,
            ),
        ] = True,
        isolation_level: Annotated[
            Literal["read_uncommitted", "read_committed"],
            Doc(
                """
            Controls how to read messages written
            transactionally.

            * `read_committed`, batch consumer will only return
            transactional messages which have been committed.

            * `read_uncommitted` (the default), batch consumer will
            return all messages, even transactional messages which have been
            aborted.

            Non-transactional messages will be returned unconditionally in
            either mode.

            Messages will always be returned in offset order. Hence, in
            `read_committed` mode, batch consumer will only return
            messages up to the last stable offset (ALSO), which is the one less
            than the offset of the first open transaction. In particular any
            messages appearing after messages belonging to ongoing transactions
            will be withheld until the relevant transaction has been completed.
            As a result, `read_committed` consumers will not be able to read up
            to the high watermark when there are in flight transactions.
            Further, when in `read_committed` the seek_to_end method will
            return the ALSO. See method docs below.
            """,
            ),
        ] = "read_uncommitted",
        batch_timeout_ms: Annotated[
            int,
            Doc(
                """
            Milliseconds spent waiting if
            data is not available in the buffer. If 0, returns immediately
            with any records that are available currently in the buffer,
            else returns empty.
            """,
            ),
        ] = 200,
        max_records: Annotated[
            int | None,
            Doc("Number of messages to consume as one batch."),
        ] = None,
        batch: Annotated[
            bool,
            Doc("Whether to consume messages in batches or not."),
        ] = False,
        listener: Annotated[
            Optional["ConsumerRebalanceListener"],
            Doc(
                """
            Optionally include listener
               callback, which will be called before and after each rebalance
               operation.
               As part of group management, the consumer will keep track of
               the list of consumers that belong to a particular group and
               will trigger a rebalance operation if one of the following
               events trigger:

               * Number of partitions change for any of the subscribed topics
               * Topic is created or deleted
               * An existing member of the consumer group dies
               * A new member is added to the consumer group

               When any of these events are triggered, the provided listener
               will be invoked first to indicate that the consumer's
               assignment has been revoked, and then again when the new
               assignment has been received. Note that this listener will
               immediately override any listener set in a previous call
               to subscribe. It is guaranteed, however, that the partitions
               revoked/assigned
               through this interface are from topics subscribed in this call.
            """,
            ),
        ] = None,
        pattern: Annotated[
            str | None,
            Doc(
                """
            Pattern to match available topics. You must provide either topics or pattern, but not both.
            """,
            ),
        ] = None,
        partitions: Annotated[
            Iterable["TopicPartition"],
            Doc(
                """
            An explicit partitions list to assign.
            You can't use 'topics' and 'partitions' in the same time.
            """,
            ),
        ] = (),
        # broker args
        dependencies: Annotated[
            Iterable["params.Depends"],
            Doc("Dependencies list (`[Depends(),]`) to apply to the subscriber."),
        ] = (),
        parser: Annotated[
            Optional["CustomCallable"],
            Doc("Parser to map original **ConsumerRecord** object to FastStream one."),
        ] = None,
        decoder: Annotated[
            Optional["CustomCallable"],
            Doc("Function to decode FastStream msg bytes body to python objects."),
        ] = None,
        middlewares: Annotated[
            Sequence["SubscriberMiddleware[Any]"],
            deprecated(
                "This option was deprecated in 0.6.0. Use router-level middlewares instead."
                "Scheduled to remove in 0.7.0",
            ),
            Doc("Subscriber middlewares to wrap incoming message processing."),
        ] = (),
        no_ack: Annotated[
            bool,
            Doc("Whether to disable **FastStream** auto acknowledgement logic or not."),
            deprecated(
                "This option was deprecated in 0.6.0 to prior to **ack_policy=AckPolicy.MANUAL**. "
                "Scheduled to remove in 0.7.0",
            ),
        ] = EMPTY,
        ack_policy: AckPolicy = EMPTY,
        no_reply: Annotated[
            bool,
            Doc(
                "Whether to disable **FastStream** RPC and Reply To auto responses or not.",
            ),
        ] = False,
        # Specification information
        title: Annotated[
            str | None,
            Doc("Specification subscriber object title."),
        ] = None,
        description: Annotated[
            str | None,
            Doc(
                "Specification subscriber object description. "
                "Uses decorated docstring as default.",
            ),
        ] = None,
        include_in_schema: Annotated[
            bool,
            Doc("Whetever to include operation in Specification schema or not."),
        ] = True,
        # FastAPI args
        response_model: Annotated[
            Any,
            Doc(
                """
                The type to use for the response.

                It could be any valid Pydantic *field* type. So, it doesn't have to
                be a Pydantic model, it could be other things, like a `list`, `dict`,
                etc.

                It will be used for:

                * Documentation: the generated OpenAPI (and the UI at `/docs`) will
                    show it as the response (JSON Schema).
                * Serialization: you could return an arbitrary object and the
                    `response_model` would be used to serialize that object into the
                    corresponding JSON.
                * Filtering: the JSON sent to the client will only contain the data
                    (fields) defined in the `response_model`. If you returned an object
                    that contains an attribute `password` but the `response_model` does
                    not include that field, the JSON sent to the client would not have
                    that `password`.
                * Validation: whatever you return will be serialized with the
                    `response_model`, converting any data as necessary to generate the
                    corresponding JSON. But if the data in the object returned is not
                    valid, that would mean a violation of the contract with the client,
                    so it's an error from the API developer. So, FastAPI will raise an
                    error and return a 500 error code (Internal Server Error).

                Read more about it in the
                [FastAPI docs for Response Model](https://fastapi.tiangolo.com/tutorial/response-model/).
                """,
            ),
        ] = Default(None),
        response_model_include: Annotated[
            Optional["IncEx"],
            Doc(
                """
                Configuration passed to Pydantic to include only certain fields in the
                response data.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#response_model_include-and-response_model_exclude).
                """,
            ),
        ] = None,
        response_model_exclude: Annotated[
            Optional["IncEx"],
            Doc(
                """
                Configuration passed to Pydantic to exclude certain fields in the
                response data.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#response_model_include-and-response_model_exclude).
                """,
            ),
        ] = None,
        response_model_by_alias: Annotated[
            bool,
            Doc(
                """
                Configuration passed to Pydantic to define if the response model
                should be serialized by alias when an alias is used.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#response_model_include-and-response_model_exclude).
                """,
            ),
        ] = True,
        response_model_exclude_unset: Annotated[
            bool,
            Doc(
                """
                Configuration passed to Pydantic to define if the response data
                should have all the fields, including the ones that were not set and
                have their default values. This is different from
                `response_model_exclude_defaults` in that if the fields are set,
                they will be included in the response, even if the value is the same
                as the default.

                When `True`, default values are omitted from the response.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#use-the-response_model_exclude_unset-parameter).
                """,
            ),
        ] = False,
        response_model_exclude_defaults: Annotated[
            bool,
            Doc(
                """
                Configuration passed to Pydantic to define if the response data
                should have all the fields, including the ones that have the same value
                as the default. This is different from `response_model_exclude_unset`
                in that if the fields are set but contain the same default values,
                they will be excluded from the response.

                When `True`, default values are omitted from the response.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#use-the-response_model_exclude_unset-parameter).
                """,
            ),
        ] = False,
        response_model_exclude_none: Annotated[
            bool,
            Doc(
                """
                Configuration passed to Pydantic to define if the response data should
                exclude fields set to `None`.

                This is much simpler (less smart) than `response_model_exclude_unset`
                and `response_model_exclude_defaults`. You probably want to use one of
                those two instead of this one, as those allow returning `None` values
                when it makes sense.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#response_model_exclude_none).
                """,
            ),
        ] = False,
    ) -> Union[
        "BatchSubscriber",
        "DefaultSubscriber",
    ]: ...

    @override
    def subscriber(
        self,
        *topics: Annotated[str, Doc("Kafka topics to consume messages from.")],
        group_id: Annotated[
            str | None,
            Doc(
                """
            Name of the consumer group to join for dynamic
            partition assignment (if enabled), and to use for fetching and
            committing offsets. If `None`, auto-partition assignment (via
            group coordinator) and offset commits are disabled.
            """,
            ),
        ] = None,
        key_deserializer: Annotated[
            Callable[[bytes], Any] | None,
            Doc(
                "Any callable that takes a raw message `bytes` "
                "key and returns a deserialized one.",
            ),
        ] = None,
        value_deserializer: Annotated[
            Callable[[bytes], Any] | None,
            Doc(
                "Any callable that takes a raw message `bytes` "
                "value and returns a deserialized value.",
            ),
        ] = None,
        fetch_max_bytes: Annotated[
            int,
            Doc(
                """
            The maximum amount of data the server should
            return for a fetch request. This is not an absolute maximum, if
            the first message in the first non-empty partition of the fetch
            is larger than this value, the message will still be returned
            to ensure that the consumer can make progress. NOTE: consumer
            performs fetches to multiple brokers in parallel so memory
            usage will depend on the number of brokers containing
            partitions for the topic.
            """,
            ),
        ] = 50 * 1024 * 1024,
        fetch_min_bytes: Annotated[
            int,
            Doc(
                """
            Minimum amount of data the server should
            return for a fetch request, otherwise wait up to
            `fetch_max_wait_ms` for more data to accumulate.
            """,
            ),
        ] = 1,
        fetch_max_wait_ms: Annotated[
            int,
            Doc(
                """
            The maximum amount of time in milliseconds
            the server will block before answering the fetch request if
            there isn't sufficient data to immediately satisfy the
            requirement given by `fetch_min_bytes`.
            """,
            ),
        ] = 500,
        max_partition_fetch_bytes: Annotated[
            int,
            Doc(
                """
            The maximum amount of data
            per-partition the server will return. The maximum total memory
            used for a request ``= #partitions * max_partition_fetch_bytes``.
            This size must be at least as large as the maximum message size
            the server allows or else it is possible for the producer to
            send messages larger than the consumer can fetch. If that
            happens, the consumer can get stuck trying to fetch a large
            message on a certain partition.
            """,
            ),
        ] = 1 * 1024 * 1024,
        auto_offset_reset: Annotated[
            Literal["latest", "earliest", "none"],
            Doc(
                """
            A policy for resetting offsets on `OffsetOutOfRangeError` errors:

            * `earliest` will move to the oldest available message
            * `latest` will move to the most recent
            * `none` will raise an exception so you can handle this case
            """,
            ),
        ] = "latest",
        auto_commit: Annotated[
            bool,
            Doc(
                """
            If `True` the consumer's offset will be
            periodically committed in the background.
            """,
            ),
            deprecated(
                "This option is deprecated and will be removed in 0.7.0 release. "
                "Please, use `ack_policy=AckPolicy.ACK_FIRST` instead."
            ),
        ] = EMPTY,
        auto_commit_interval_ms: Annotated[
            int,
            Doc(
                """
            Milliseconds between automatic
            offset commits, if `auto_commit` is `True`.""",
            ),
        ] = 5 * 1000,
        check_crcs: Annotated[
            bool,
            Doc(
                """
            Automatically check the CRC32 of the records
            consumed. This ensures no on-the-wire or on-disk corruption to
            the messages occurred. This check adds some overhead, so it may
            be disabled in cases seeking extreme performance.
            """,
            ),
        ] = True,
        partition_assignment_strategy: Annotated[
            Sequence["AbstractPartitionAssignor"],
            Doc(
                """
            List of objects to use to
            distribute partition ownership amongst consumer instances when
            group management is used. This preference is implicit in the order
            of the strategies in the list. When assignment strategy changes:
            to support a change to the assignment strategy, new versions must
            enable support both for the old assignment strategy and the new
            one. The coordinator will choose the old assignment strategy until
            all members have been updated. Then it will choose the new
            strategy.
            """,
            ),
        ] = (RoundRobinPartitionAssignor,),
        max_poll_interval_ms: Annotated[
            int,
            Doc(
                """
            Maximum allowed time between calls to
            consume messages in batches. If this interval
            is exceeded the consumer is considered failed and the group will
            rebalance in order to reassign the partitions to another consumer
            group member. If API methods block waiting for messages, that time
            does not count against this timeout.
            """,
            ),
        ] = 5 * 60 * 1000,
        rebalance_timeout_ms: Annotated[
            int | None,
            Doc(
                """
            The maximum time server will wait for this
            consumer to rejoin the group in a case of rebalance. In Java client
            this behaviour is bound to `max.poll.interval.ms` configuration,
            but as ``aiokafka`` will rejoin the group in the background, we
            decouple this setting to allow finer tuning by users that use
            `ConsumerRebalanceListener` to delay rebalacing. Defaults
            to ``session_timeout_ms``
            """,
            ),
        ] = None,
        session_timeout_ms: Annotated[
            int,
            Doc(
                """
            Client group session and failure detection
            timeout. The consumer sends periodic heartbeats
            (`heartbeat.interval.ms`) to indicate its liveness to the broker.
            If no hearts are received by the broker for a group member within
            the session timeout, the broker will remove the consumer from the
            group and trigger a rebalance. The allowed range is configured with
            the **broker** configuration properties
            `group.min.session.timeout.ms` and `group.max.session.timeout.ms`.
            """,
            ),
        ] = 10 * 1000,
        heartbeat_interval_ms: Annotated[
            int,
            Doc(
                """
            The expected time in milliseconds
            between heartbeats to the consumer coordinator when using
            Kafka's group management feature. Heartbeats are used to ensure
            that the consumer's session stays active and to facilitate
            rebalancing when new consumers join or leave the group. The
            value must be set lower than `session_timeout_ms`, but typically
            should be set no higher than 1/3 of that value. It can be
            adjusted even lower to control the expected time for normal
            rebalances.
            """,
            ),
        ] = 3 * 1000,
        consumer_timeout_ms: Annotated[
            int,
            Doc(
                """
            Maximum wait timeout for background fetching
            routine. Mostly defines how fast the system will see rebalance and
            request new data for new partitions.
            """,
            ),
        ] = 200,
        max_poll_records: Annotated[
            int | None,
            Doc(
                """
            The maximum number of records returned in a
            single call by batch consumer. Has no limit by default.
            """,
            ),
        ] = None,
        exclude_internal_topics: Annotated[
            bool,
            Doc(
                """
            Whether records from internal topics
            (such as offsets) should be exposed to the consumer. If set to True
            the only way to receive records from an internal topic is
            subscribing to it.
            """,
            ),
        ] = True,
        isolation_level: Annotated[
            Literal["read_uncommitted", "read_committed"],
            Doc(
                """
            Controls how to read messages written
            transactionally.

            * `read_committed`, batch consumer will only return
            transactional messages which have been committed.

            * `read_uncommitted` (the default), batch consumer will
            return all messages, even transactional messages which have been
            aborted.

            Non-transactional messages will be returned unconditionally in
            either mode.

            Messages will always be returned in offset order. Hence, in
            `read_committed` mode, batch consumer will only return
            messages up to the last stable offset (ALSO), which is the one less
            than the offset of the first open transaction. In particular any
            messages appearing after messages belonging to ongoing transactions
            will be withheld until the relevant transaction has been completed.
            As a result, `read_committed` consumers will not be able to read up
            to the high watermark when there are in flight transactions.
            Further, when in `read_committed` the seek_to_end method will
            return the ALSO. See method docs below.
            """,
            ),
        ] = "read_uncommitted",
        batch_timeout_ms: Annotated[
            int,
            Doc(
                """
            Milliseconds spent waiting if
            data is not available in the buffer. If 0, returns immediately
            with any records that are available currently in the buffer,
            else returns empty.
            """,
            ),
        ] = 200,
        max_records: Annotated[
            int | None,
            Doc("Number of messages to consume as one batch."),
        ] = None,
        batch: Annotated[
            bool,
            Doc("Whether to consume messages in batches or not."),
        ] = False,
        listener: Annotated[
            Optional["ConsumerRebalanceListener"],
            Doc(
                """
            Optionally include listener
               callback, which will be called before and after each rebalance
               operation.
               As part of group management, the consumer will keep track of
               the list of consumers that belong to a particular group and
               will trigger a rebalance operation if one of the following
               events trigger:

               * Number of partitions change for any of the subscribed topics
               * Topic is created or deleted
               * An existing member of the consumer group dies
               * A new member is added to the consumer group

               When any of these events are triggered, the provided listener
               will be invoked first to indicate that the consumer's
               assignment has been revoked, and then again when the new
               assignment has been received. Note that this listener will
               immediately override any listener set in a previous call
               to subscribe. It is guaranteed, however, that the partitions
               revoked/assigned
               through this interface are from topics subscribed in this call.
            """,
            ),
        ] = None,
        pattern: Annotated[
            str | None,
            Doc(
                """
            Pattern to match available topics. You must provide either topics or pattern, but not both.
            """,
            ),
        ] = None,
        partitions: Annotated[
            Iterable["TopicPartition"],
            Doc(
                """
            An explicit partitions list to assign.
            You can't use 'topics' and 'partitions' in the same time.
            """,
            ),
        ] = (),
        # broker args
        dependencies: Annotated[
            Iterable["params.Depends"],
            Doc("Dependencies list (`[Depends(),]`) to apply to the subscriber."),
        ] = (),
        parser: Annotated[
            Optional["CustomCallable"],
            Doc("Parser to map original **ConsumerRecord** object to FastStream one."),
        ] = None,
        decoder: Annotated[
            Optional["CustomCallable"],
            Doc("Function to decode FastStream msg bytes body to python objects."),
        ] = None,
        middlewares: Annotated[
            Sequence["SubscriberMiddleware[Any]"],
            deprecated(
                "This option was deprecated in 0.6.0. Use router-level middlewares instead."
                "Scheduled to remove in 0.7.0",
            ),
            Doc("Subscriber middlewares to wrap incoming message processing."),
        ] = (),
        no_ack: Annotated[
            bool,
            Doc("Whether to disable **FastStream** auto acknowledgement logic or not."),
            deprecated(
                "This option was deprecated in 0.6.0 to prior to **ack_policy=AckPolicy.MANUAL**. "
                "Scheduled to remove in 0.7.0",
            ),
        ] = EMPTY,
        ack_policy: AckPolicy = EMPTY,
        no_reply: Annotated[
            bool,
            Doc(
                "Whether to disable **FastStream** RPC and Reply To auto responses or not.",
            ),
        ] = False,
        # Specification information
        title: Annotated[
            str | None,
            Doc("Specification subscriber object title."),
        ] = None,
        description: Annotated[
            str | None,
            Doc(
                "Specification subscriber object description. "
                "Uses decorated docstring as default.",
            ),
        ] = None,
        include_in_schema: Annotated[
            bool,
            Doc("Whetever to include operation in Specification schema or not."),
        ] = True,
        # FastAPI args
        response_model: Annotated[
            Any,
            Doc(
                """
                The type to use for the response.

                It could be any valid Pydantic *field* type. So, it doesn't have to
                be a Pydantic model, it could be other things, like a `list`, `dict`,
                etc.

                It will be used for:

                * Documentation: the generated OpenAPI (and the UI at `/docs`) will
                    show it as the response (JSON Schema).
                * Serialization: you could return an arbitrary object and the
                    `response_model` would be used to serialize that object into the
                    corresponding JSON.
                * Filtering: the JSON sent to the client will only contain the data
                    (fields) defined in the `response_model`. If you returned an object
                    that contains an attribute `password` but the `response_model` does
                    not include that field, the JSON sent to the client would not have
                    that `password`.
                * Validation: whatever you return will be serialized with the
                    `response_model`, converting any data as necessary to generate the
                    corresponding JSON. But if the data in the object returned is not
                    valid, that would mean a violation of the contract with the client,
                    so it's an error from the API developer. So, FastAPI will raise an
                    error and return a 500 error code (Internal Server Error).

                Read more about it in the
                [FastAPI docs for Response Model](https://fastapi.tiangolo.com/tutorial/response-model/).
                """,
            ),
        ] = Default(None),
        response_model_include: Annotated[
            Optional["IncEx"],
            Doc(
                """
                Configuration passed to Pydantic to include only certain fields in the
                response data.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#response_model_include-and-response_model_exclude).
                """,
            ),
        ] = None,
        response_model_exclude: Annotated[
            Optional["IncEx"],
            Doc(
                """
                Configuration passed to Pydantic to exclude certain fields in the
                response data.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#response_model_include-and-response_model_exclude).
                """,
            ),
        ] = None,
        response_model_by_alias: Annotated[
            bool,
            Doc(
                """
                Configuration passed to Pydantic to define if the response model
                should be serialized by alias when an alias is used.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#response_model_include-and-response_model_exclude).
                """,
            ),
        ] = True,
        response_model_exclude_unset: Annotated[
            bool,
            Doc(
                """
                Configuration passed to Pydantic to define if the response data
                should have all the fields, including the ones that were not set and
                have their default values. This is different from
                `response_model_exclude_defaults` in that if the fields are set,
                they will be included in the response, even if the value is the same
                as the default.

                When `True`, default values are omitted from the response.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#use-the-response_model_exclude_unset-parameter).
                """,
            ),
        ] = False,
        response_model_exclude_defaults: Annotated[
            bool,
            Doc(
                """
                Configuration passed to Pydantic to define if the response data
                should have all the fields, including the ones that have the same value
                as the default. This is different from `response_model_exclude_unset`
                in that if the fields are set but contain the same default values,
                they will be excluded from the response.

                When `True`, default values are omitted from the response.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#use-the-response_model_exclude_unset-parameter).
                """,
            ),
        ] = False,
        response_model_exclude_none: Annotated[
            bool,
            Doc(
                """
                Configuration passed to Pydantic to define if the response data should
                exclude fields set to `None`.

                This is much simpler (less smart) than `response_model_exclude_unset`
                and `response_model_exclude_defaults`. You probably want to use one of
                those two instead of this one, as those allow returning `None` values
                when it makes sense.

                Read more about it in the
                [FastAPI docs for Response Model - Return Type](https://fastapi.tiangolo.com/tutorial/response-model/#response_model_exclude_none).
                """,
            ),
        ] = False,
        max_workers: Annotated[
            int,
            Doc(
                "Maximum number of messages being processed concurrently. With "
                "`auto_commit=False` processing is concurrent between partitions and "
                "sequential within a partition. With `auto_commit=False` maximum "
                "concurrency is achieved when total number of workers across all "
                "application instances running workers in the same consumer group "
                "is equal to the number of partitions in the topic.",
            ),
        ] = 1,
    ) -> Union[
        "BatchSubscriber",
        "DefaultSubscriber",
        "ConcurrentDefaultSubscriber",
        "ConcurrentBetweenPartitionsSubscriber",
    ]:
        subscriber = super().subscriber(
            *topics,
            group_id=group_id,
            max_workers=max_workers,
            key_deserializer=key_deserializer,
            value_deserializer=value_deserializer,
            fetch_max_wait_ms=fetch_max_wait_ms,
            fetch_max_bytes=fetch_max_bytes,
            fetch_min_bytes=fetch_min_bytes,
            max_partition_fetch_bytes=max_partition_fetch_bytes,
            auto_offset_reset=auto_offset_reset,
            auto_commit=auto_commit,
            auto_commit_interval_ms=auto_commit_interval_ms,
            check_crcs=check_crcs,
            partition_assignment_strategy=partition_assignment_strategy,
            max_poll_interval_ms=max_poll_interval_ms,
            rebalance_timeout_ms=rebalance_timeout_ms,
            session_timeout_ms=session_timeout_ms,
            heartbeat_interval_ms=heartbeat_interval_ms,
            consumer_timeout_ms=consumer_timeout_ms,
            max_poll_records=max_poll_records,
            exclude_internal_topics=exclude_internal_topics,
            isolation_level=isolation_level,
            batch=batch,
            max_records=max_records,
            batch_timeout_ms=batch_timeout_ms,
            listener=listener,
            pattern=pattern,
            partitions=partitions,
            # broker args
            dependencies=dependencies,
            parser=parser,
            decoder=decoder,
            middlewares=middlewares,
            ack_policy=ack_policy,
            no_ack=no_ack,
            no_reply=no_reply,
            title=title,
            description=description,
            include_in_schema=include_in_schema,
            # FastAPI args
            response_model=response_model,
            response_model_include=response_model_include,
            response_model_exclude=response_model_exclude,
            response_model_by_alias=response_model_by_alias,
            response_model_exclude_unset=response_model_exclude_unset,
            response_model_exclude_defaults=response_model_exclude_defaults,
            response_model_exclude_none=response_model_exclude_none,
        )

        if batch:
            return cast("BatchSubscriber", subscriber)
        if max_workers > 1:
            if auto_commit:
                return cast("ConcurrentDefaultSubscriber", subscriber)
            return cast("ConcurrentBetweenPartitionsSubscriber", subscriber)
        return cast("DefaultSubscriber", subscriber)

    @overload  # type: ignore[override]
    def publisher(
        self,
        topic: Annotated[
            str,
            Doc("Topic where the message will be published."),
        ],
        *,
        key: Annotated[
            bytes | Any | None,
            Doc(
                """
            A key to associate with the message. Can be used to
            determine which partition to send the message to. If partition
            is `None` (and producer's partitioner config is left as default),
            then messages with the same key will be delivered to the same
            partition (but if key is `None`, partition is chosen randomly).
            Must be type `bytes`, or be serializable to bytes via configured
            `key_serializer`.
            """,
            ),
        ] = None,
        partition: Annotated[
            int | None,
            Doc(
                """
            Specify a partition. If not set, the partition will be
            selected using the configured `partitioner`.
            """,
            ),
        ] = None,
        headers: Annotated[
            dict[str, str] | None,
            Doc(
                "Message headers to store metainformation. "
                "**content-type** and **correlation_id** will be set automatically by framework anyway. "
                "Can be overridden by `publish.headers` if specified.",
            ),
        ] = None,
        reply_to: Annotated[
            str,
            Doc("Topic name to send response."),
        ] = "",
        batch: Annotated[
            Literal[False],
            Doc("Whether to send messages in batches or not."),
        ] = False,
        # basic args
        middlewares: Annotated[
            Sequence["PublisherMiddleware"],
            deprecated(
                "This option was deprecated in 0.6.0. Use router-level middlewares instead."
                "Scheduled to remove in 0.7.0",
            ),
            Doc("Publisher middlewares to wrap outgoing messages."),
        ] = (),
        # Specification args
        title: Annotated[
            str | None,
            Doc("Specification publisher object title."),
        ] = None,
        description: Annotated[
            str | None,
            Doc("Specification publisher object description."),
        ] = None,
        schema: Annotated[
            Any | None,
            Doc(
                "Specification publishing message type. "
                "Should be any python-native object annotation or `pydantic.BaseModel`.",
            ),
        ] = None,
        include_in_schema: Annotated[
            bool,
            Doc("Whetever to include operation in Specification schema or not."),
        ] = True,
    ) -> "DefaultPublisher": ...

    @overload
    def publisher(
        self,
        topic: Annotated[
            str,
            Doc("Topic where the message will be published."),
        ],
        *,
        key: Annotated[
            bytes | Any | None,
            Doc(
                """
            A key to associate with the message. Can be used to
            determine which partition to send the message to. If partition
            is `None` (and producer's partitioner config is left as default),
            then messages with the same key will be delivered to the same
            partition (but if key is `None`, partition is chosen randomly).
            Must be type `bytes`, or be serializable to bytes via configured
            `key_serializer`.
            """,
            ),
        ] = None,
        partition: Annotated[
            int | None,
            Doc(
                """
            Specify a partition. If not set, the partition will be
            selected using the configured `partitioner`.
            """,
            ),
        ] = None,
        headers: Annotated[
            dict[str, str] | None,
            Doc(
                "Message headers to store metainformation. "
                "**content-type** and **correlation_id** will be set automatically by framework anyway. "
                "Can be overridden by `publish.headers` if specified.",
            ),
        ] = None,
        reply_to: Annotated[
            str,
            Doc("Topic name to send response."),
        ] = "",
        batch: Annotated[
            Literal[True],
            Doc("Whether to send messages in batches or not."),
        ],
        # basic args
        middlewares: Annotated[
            Sequence["PublisherMiddleware"],
            deprecated(
                "This option was deprecated in 0.6.0. Use router-level middlewares instead."
                "Scheduled to remove in 0.7.0",
            ),
            Doc("Publisher middlewares to wrap outgoing messages."),
        ] = (),
        # Specification args
        title: Annotated[
            str | None,
            Doc("Specification publisher object title."),
        ] = None,
        description: Annotated[
            str | None,
            Doc("Specification publisher object description."),
        ] = None,
        schema: Annotated[
            Any | None,
            Doc(
                "Specification publishing message type. "
                "Should be any python-native object annotation or `pydantic.BaseModel`.",
            ),
        ] = None,
        include_in_schema: Annotated[
            bool,
            Doc("Whetever to include operation in Specification schema or not."),
        ] = True,
    ) -> "BatchPublisher": ...

    @overload
    def publisher(
        self,
        topic: Annotated[
            str,
            Doc("Topic where the message will be published."),
        ],
        *,
        key: Annotated[
            bytes | Any | None,
            Doc(
                """
            A key to associate with the message. Can be used to
            determine which partition to send the message to. If partition
            is `None` (and producer's partitioner config is left as default),
            then messages with the same key will be delivered to the same
            partition (but if key is `None`, partition is chosen randomly).
            Must be type `bytes`, or be serializable to bytes via configured
            `key_serializer`.
            """,
            ),
        ] = None,
        partition: Annotated[
            int | None,
            Doc(
                """
            Specify a partition. If not set, the partition will be
            selected using the configured `partitioner`.
            """,
            ),
        ] = None,
        headers: Annotated[
            dict[str, str] | None,
            Doc(
                "Message headers to store metainformation. "
                "**content-type** and **correlation_id** will be set automatically by framework anyway. "
                "Can be overridden by `publish.headers` if specified.",
            ),
        ] = None,
        reply_to: Annotated[
            str,
            Doc("Topic name to send response."),
        ] = "",
        batch: Annotated[
            bool,
            Doc("Whether to send messages in batches or not."),
        ] = False,
        # basic args
        middlewares: Annotated[
            Sequence["PublisherMiddleware"],
            deprecated(
                "This option was deprecated in 0.6.0. Use router-level middlewares instead."
                "Scheduled to remove in 0.7.0",
            ),
            Doc("Publisher middlewares to wrap outgoing messages."),
        ] = (),
        # Specification args
        title: Annotated[
            str | None,
            Doc("Specification publisher object title."),
        ] = None,
        description: Annotated[
            str | None,
            Doc("Specification publisher object description."),
        ] = None,
        schema: Annotated[
            Any | None,
            Doc(
                "Specification publishing message type. "
                "Should be any python-native object annotation or `pydantic.BaseModel`.",
            ),
        ] = None,
        include_in_schema: Annotated[
            bool,
            Doc("Whetever to include operation in Specification schema or not."),
        ] = True,
    ) -> Union[
        "BatchPublisher",
        "DefaultPublisher",
    ]: ...

    @override
    def publisher(
        self,
        topic: Annotated[
            str,
            Doc("Topic where the message will be published."),
        ],
        *,
        key: Annotated[
            bytes | Any | None,
            Doc(
                """
            A key to associate with the message. Can be used to
            determine which partition to send the message to. If partition
            is `None` (and producer's partitioner config is left as default),
            then messages with the same key will be delivered to the same
            partition (but if key is `None`, partition is chosen randomly).
            Must be type `bytes`, or be serializable to bytes via configured
            `key_serializer`.
            """,
            ),
        ] = None,
        partition: Annotated[
            int | None,
            Doc(
                """
            Specify a partition. If not set, the partition will be
            selected using the configured `partitioner`.
            """,
            ),
        ] = None,
        headers: Annotated[
            dict[str, str] | None,
            Doc(
                "Message headers to store metainformation. "
                "**content-type** and **correlation_id** will be set automatically by framework anyway. "
                "Can be overridden by `publish.headers` if specified.",
            ),
        ] = None,
        reply_to: Annotated[
            str,
            Doc("Topic name to send response."),
        ] = "",
        batch: Annotated[
            bool,
            Doc("Whether to send messages in batches or not."),
        ] = False,
        # basic args
        middlewares: Annotated[
            Sequence["PublisherMiddleware"],
            deprecated(
                "This option was deprecated in 0.6.0. Use router-level middlewares instead."
                "Scheduled to remove in 0.7.0",
            ),
            Doc("Publisher middlewares to wrap outgoing messages."),
        ] = (),
        # Specification args
        title: Annotated[
            str | None,
            Doc("Specification publisher object title."),
        ] = None,
        description: Annotated[
            str | None,
            Doc("Specification publisher object description."),
        ] = None,
        schema: Annotated[
            Any | None,
            Doc(
                "Specification publishing message type. "
                "Should be any python-native object annotation or `pydantic.BaseModel`.",
            ),
        ] = None,
        include_in_schema: Annotated[
            bool,
            Doc("Whetever to include operation in Specification schema or not."),
        ] = True,
    ) -> Union[
        "BatchPublisher",
        "DefaultPublisher",
    ]:
        return self.broker.publisher(
            topic=topic,
            key=key,
            partition=partition,
            headers=headers,
            batch=batch,
            reply_to=reply_to,
            # broker options
            middlewares=middlewares,
            # Specification options
            title=title,
            description=description,
            schema=schema,
            include_in_schema=include_in_schema,
        )
