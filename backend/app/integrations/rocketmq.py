"""RocketMQ 5.x gRPC transport for the existing analysis message contract.

Business validation, retry decisions, and message lease renewal scheduling belong
to the worker. This module deliberately keeps the received SDK message object so
ACK uses the receipt handle updated by ``change_invisible_duration``.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping


ANALYSIS_TOPIC = "video-analysis-topic"
DEAD_TOPIC = "video-analysis-dead-topic"
ANALYSIS_GROUP = "video-analysis-consumer"


def _sdk_configuration(endpoints: str, *, request_timeout: int = 3) -> Any:
    from rocketmq import ClientConfiguration, Credentials

    return ClientConfiguration(endpoints, Credentials(), request_timeout=request_timeout)


def decode_task(message: Any) -> dict[str, Any]:
    """Decode one UTF-8 JSON object without changing legacy DTO field values."""
    try:
        payload = json.loads(message.body.decode("utf-8"))
    except (AttributeError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("RocketMQ analysis message is not UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("RocketMQ analysis message must be a JSON object")
    return payload


class RocketMQProducer:
    def __init__(
        self,
        endpoints: str,
        *,
        topics: tuple[str, ...] = (ANALYSIS_TOPIC, DEAD_TOPIC),
        client: Any | None = None,
        message_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not endpoints.strip():
            raise ValueError("RocketMQ gRPC endpoints are required")
        if not topics or any(not topic.strip() for topic in topics):
            raise ValueError("RocketMQ topics are required")
        self._topics = topics
        if client is None or message_factory is None:
            from rocketmq import Message, Producer

            if client is None:
                client = Producer(_sdk_configuration(endpoints), topics=set(topics))
            if message_factory is None:
                message_factory = Message
        self._client = client
        self._message_factory = message_factory

    def startup(self) -> None:
        self._client.startup()

    def shutdown(self) -> None:
        self._client.shutdown()

    def send_task(
        self, payload: Mapping[str, Any], *, topic: str | None = None
    ) -> Any:
        """Send original camelCase DTO as UTF-8 JSON; return SDK receipt."""
        selected_topic = topic or self._topics[0]
        if selected_topic not in self._topics:
            raise ValueError(f"RocketMQ topic not configured: {selected_topic}")
        message = self._message_factory()
        message.topic = selected_topic
        message.body = json.dumps(
            dict(payload), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return self._client.send(message)


class RocketMQConsumer:
    def __init__(
        self,
        endpoints: str,
        *,
        topic: str = ANALYSIS_TOPIC,
        group: str = ANALYSIS_GROUP,
        invisible_duration: int = 90,
        await_duration: int = 10,
        request_timeout_seconds: int = 30,
        client: Any | None = None,
    ) -> None:
        if not endpoints.strip() or not topic.strip() or not group.strip():
            raise ValueError("RocketMQ endpoints, topic and group are required")
        # Broker 5.3.4 rejects an invisible time below 10,000 ms (status 40011).
        if invisible_duration < 10 or await_duration < 1 or request_timeout_seconds < 1:
            raise ValueError("RocketMQ invisibility must be at least 10 seconds")
        if client is None:
            from rocketmq import FilterExpression, SimpleConsumer

            client = SimpleConsumer(
                # Receive RPC timeout is request_timeout + await_duration in
                # SDK 5.1.2. The default 3s margin proved too short against
                # Broker 5.3.4 under local long polling/redelivery.
                _sdk_configuration(endpoints, request_timeout=request_timeout_seconds),
                group,
                {topic: FilterExpression()},
                await_duration=await_duration,
            )
        self._client = client
        self._invisible_duration = invisible_duration
        self._rpc_timeout_seconds = request_timeout_seconds

    @property
    def invisible_duration(self) -> int:
        return self._invisible_duration

    @property
    def rpc_timeout_seconds(self) -> int:
        return self._rpc_timeout_seconds

    def startup(self) -> None:
        self._client.startup()

    def shutdown(self) -> None:
        self._client.shutdown()

    def receive_one(self) -> Any | None:
        """Claim at most one message because each worker has one execution slot."""
        messages = self._client.receive(1, self._invisible_duration)
        if not messages:
            return None
        if len(messages) != 1:
            raise RuntimeError("RocketMQ returned more messages than requested")
        return messages[0]

    def renew(self, message: Any, seconds: int) -> None:
        """Synchronously renew this message; SDK replaces its receipt handle."""
        if seconds < 10:
            raise ValueError("RocketMQ invisibility must be at least 10 seconds")
        self._client.change_invisible_duration(message, seconds)

    def ack(self, message: Any) -> None:
        """ACK with the current receipt handle after durable business completion."""
        self._client.ack(message)
