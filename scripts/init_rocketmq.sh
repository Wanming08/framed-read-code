#!/usr/bin/env bash
set -euo pipefail

# Run after `docker compose up -d rmqnamesrv rmqbroker`.
cd "$(dirname "$0")/.."

analysis_topic="${ROCKETMQ_ANALYSIS_TOPIC:-video-analysis-topic}"
dead_topic="${ROCKETMQ_ANALYSIS_DEAD_TOPIC:-video-analysis-dead-topic}"
analysis_group="${ROCKETMQ_ANALYSIS_GROUP:-video-analysis-consumer}"

mqadmin() {
  docker compose exec -T rmqbroker sh mqadmin "$@"
}

broker_ready=0
for _ in {1..20}; do
  cluster="$(mqadmin clusterList -n rmqnamesrv:9876 2>&1 || true)"
  if [[ "$cluster" == *"broker-a"* ]]; then
    broker_ready=1
    break
  fi
  sleep 2
done
if [[ "$broker_ready" != "1" ]]; then
  echo "RocketMQ Broker is not registered in NameServer." >&2
  exit 1
fi

for topic in "$analysis_topic" "$dead_topic"; do
  create_result="$(mqadmin updateTopic -n rmqnamesrv:9876 -c DefaultCluster -t "$topic" 2>&1)"
  if [[ "$create_result" != *"success"* ]]; then
    printf '%s\n' "$create_result" >&2
    exit 1
  fi
  route="$(mqadmin topicRoute -n rmqnamesrv:9876 -t "$topic" 2>&1)"
  if [[ "$route" != *'"brokerAddrs"'* ]]; then
    printf '%s\n' "$route" >&2
    exit 1
  fi
done

# Java maxReconsumeTimes=2 means at most two Broker redeliveries.
for group in "$analysis_group"; do
  group_result="$(mqadmin updateSubGroup -n rmqnamesrv:9876 -c DefaultCluster -g "$group" -r 2 2>&1)"
  if [[ "$group_result" != *"success"* ]]; then
    printf '%s\n' "$group_result" >&2
    exit 1
  fi
done
echo "RocketMQ topics and consumer groups configured."
