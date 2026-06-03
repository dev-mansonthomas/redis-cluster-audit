#!/bin/bash
# Initialise the Redis Cluster after docker-compose up.
# Run once — idempotent: exits cleanly if the cluster is already formed.

set -e

echo "Waiting for Redis nodes to be ready..."
for port in 6777 6778 6779; do
    until redis-cli -p $port PING 2>/dev/null | grep -q PONG; do
        sleep 0.5
    done
    echo "  Node :$port is up"
done

# Check if the cluster is already formed
CLUSTER_STATE=$(redis-cli -p 6777 CLUSTER INFO | grep "cluster_state" | tr -d '\r')
if [ "$CLUSTER_STATE" = "cluster_state:ok" ]; then
    echo "Cluster already initialised — nothing to do."
    exit 0
fi

echo "Creating cluster (using fixed internal IPs 172.29.0.10-12)..."
# We run the create command from inside a container so nodes can reach each
# other via the Docker bridge network (172.29.0.x).
# The Python clients use address_remap to translate those IPs → 127.0.0.1:port.
docker exec redis-node-1 redis-cli --cluster create \
    172.29.0.10:6777 \
    172.29.0.11:6778 \
    172.29.0.12:6779 \
    --cluster-replicas 0 --cluster-yes

echo ""
echo "Cluster ready. Topology:"
redis-cli -p 6777 CLUSTER NODES
