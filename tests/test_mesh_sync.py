"""Comprehensive test suite for edge peer-to-peer (P2P) ad-hoc synchronization mesh.

Validates:
1. P2P Mesh Node initialization, state management, and neighbor discovery.
2. Direct peer-to-peer connection establishment and mutual authentication via peer tokens.
3. Gossip protocol propagation of cadastral transactions across decentralized field surveyors.
4. Anti-entropy sync exchange between pairs of peers (vector clock or version-based reconciliation).
5. Dynamic mesh topology partitioning, re-joining, and multi-hop gossip convergence.
6. Loop detection and deduplication of gossiped messages via message ID cache.
7. Graceful offline disconnected operation with eventual convergence once peer link is restored.
"""

from __future__ import annotations

import copy
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
import pytest

from backend.edge.mesh import (
    GossipMessage,
    GossipMessageType,
    MeshLinkState,
    MeshNodeStatus,
    P2PMeshCoordinator,
    P2PMeshNode,
    PeerDescriptor,
    VectorClock,
)


# =====================================================================
# Fixtures
# =====================================================================

@pytest.fixture
def surveyor_nodes() -> List[P2PMeshNode]:
    """Create three simulated field surveyor nodes forming an ad-hoc mesh."""
    n1 = P2PMeshNode(
        node_id="surveyor-pune-01",
        node_name="Surveyor 1 (Wagholi)",
        district="Pune",
        ip_address="192.168.43.10",
        port=9001,
    )
    n2 = P2PMeshNode(
        node_id="surveyor-pune-02",
        node_name="Surveyor 2 (Kalyani Nagar)",
        district="Pune",
        ip_address="192.168.43.11",
        port=9002,
    )
    n3 = P2PMeshNode(
        node_id="surveyor-pune-03",
        node_name="Surveyor 3 (Viman Nagar)",
        district="Pune",
        ip_address="192.168.43.12",
        port=9003,
    )
    return [n1, n2, n3]


# =====================================================================
# 1. Vector Clock Tests
# =====================================================================

class TestVectorClock:
    """Tests for distributed causal ordering and vector clocks."""

    def test_vector_clock_initialization_and_increment(self):
        vc = VectorClock()
        assert vc.get_clock("node-1") == 0

        vc.increment("node-1")
        assert vc.get_clock("node-1") == 1
        vc.increment("node-1")
        assert vc.get_clock("node-1") == 2

    def test_vector_clock_merge_takes_elementwise_maximum(self):
        vc1 = VectorClock({"A": 2, "B": 4, "C": 1})
        vc2 = VectorClock({"A": 3, "B": 2, "D": 5})

        merged = vc1.merge(vc2)
        assert merged.get_clock("A") == 3
        assert merged.get_clock("B") == 4
        assert merged.get_clock("C") == 1
        assert merged.get_clock("D") == 5

    def test_vector_clock_causality_comparison(self):
        vc_ancestor = VectorClock({"A": 1, "B": 1})
        vc_descendant = VectorClock({"A": 2, "B": 1})
        vc_concurrent = VectorClock({"A": 1, "B": 2})

        assert vc_ancestor.is_dominated_by(vc_descendant) is True
        assert vc_descendant.is_dominated_by(vc_ancestor) is False
        assert vc_descendant.is_concurrent_with(vc_concurrent) is True


# =====================================================================
# 2. P2P Mesh Node Discovery & Peering Tests
# =====================================================================

class TestP2PMeshPeering:
    """Tests for ad-hoc peer connection, link state, and authentication."""

    def test_node_initialization(self, surveyor_nodes: List[P2PMeshNode]):
        n1 = surveyor_nodes[0]
        assert n1.node_id == "surveyor-pune-01"
        assert n1.status == MeshNodeStatus.ONLINE
        assert len(n1.list_peers()) == 0

    def test_add_and_authenticate_peer(self, surveyor_nodes: List[P2PMeshNode]):
        n1, n2 = surveyor_nodes[0], surveyor_nodes[1]

        # n1 discovers and connects to n2
        peer_desc = PeerDescriptor(
            node_id=n2.node_id,
            node_name=n2.node_name,
            ip_address=n2.ip_address,
            port=n2.port,
            district=n2.district,
            shared_secret="khasra-mesh-token-pune",
        )
        success = n1.connect_peer(peer_desc, auth_secret="khasra-mesh-token-pune")
        assert success is True

        peer = n1.get_peer(n2.node_id)
        assert peer is not None
        assert peer.link_state == MeshLinkState.CONNECTED

    def test_peer_authentication_failure(self, surveyor_nodes: List[P2PMeshNode]):
        n1, n2 = surveyor_nodes[0], surveyor_nodes[1]
        peer_desc = PeerDescriptor(
            node_id=n2.node_id,
            node_name=n2.node_name,
            ip_address=n2.ip_address,
            port=n2.port,
            district=n2.district,
            shared_secret="secret-token-xyz",
        )
        # Attempt connection with incorrect secret
        success = n1.connect_peer(peer_desc, auth_secret="wrong-token")
        assert success is False
        peer = n1.get_peer(n2.node_id)
        assert peer is None or peer.link_state == MeshLinkState.DISCONNECTED

    def test_disconnect_and_reconnect_peer(self, surveyor_nodes: List[P2PMeshNode]):
        n1, n2 = surveyor_nodes[0], surveyor_nodes[1]
        peer_desc = n2.as_descriptor(shared_secret="token-123")
        n1.connect_peer(peer_desc, auth_secret="token-123")
        assert n1.get_peer(n2.node_id).link_state == MeshLinkState.CONNECTED

        # Disconnect (e.g. WiFi Direct link out of range)
        n1.disconnect_peer(n2.node_id, reason="Link out of range")
        assert n1.get_peer(n2.node_id).link_state == MeshLinkState.DISCONNECTED

        # Reconnect
        n1.connect_peer(peer_desc, auth_secret="token-123")
        assert n1.get_peer(n2.node_id).link_state == MeshLinkState.CONNECTED


# =====================================================================
# 3. Gossip Protocol & Message Propagation Tests
# =====================================================================

class TestGossipPropagation:
    """Tests for multi-hop epidemic gossip propagation across peers."""

    def test_local_transaction_broadcast_creates_gossip_message(
        self, surveyor_nodes: List[P2PMeshNode]
    ):
        n1 = surveyor_nodes[0]
        mutation = {
            "parcel_id": "KHASRA-301",
            "operation": "SURVEYOR_MEASUREMENT",
            "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
            "area_sqm": 100.0,
        }

        msg = n1.broadcast_mutation(
            entity_id="KHASRA-301",
            payload=mutation,
        )

        assert isinstance(msg, GossipMessage)
        assert msg.origin_node_id == n1.node_id
        assert msg.entity_id == "KHASRA-301"
        assert msg.hops == 0
        assert msg.msg_id in n1.get_seen_message_ids()

    def test_gossip_dissemination_two_nodes(self, surveyor_nodes: List[P2PMeshNode]):
        n1, n2 = surveyor_nodes[0], surveyor_nodes[1]
        # Interconnect n1 <-> n2
        token = "mesh-shared-secret"
        n1.connect_peer(n2.as_descriptor(token), token)
        n2.connect_peer(n1.as_descriptor(token), token)

        # n1 records field mutation
        mutation = {"parcel_id": "KHASRA-401", "area_sqm": 2500.0}
        msg = n1.broadcast_mutation("KHASRA-401", mutation)

        # n2 receives message from n1
        received = n2.receive_gossip_message(msg)
        assert received is True
        assert "KHASRA-401" in n2.get_local_store()
        assert n2.get_local_store()["KHASRA-401"]["area_sqm"] == 2500.0
        assert msg.msg_id in n2.get_seen_message_ids()

    def test_gossip_deduplication_and_loop_prevention(
        self, surveyor_nodes: List[P2PMeshNode]
    ):
        n1, n2 = surveyor_nodes[0], surveyor_nodes[1]
        token = "mesh-shared-secret"
        n1.connect_peer(n2.as_descriptor(token), token)
        n2.connect_peer(n1.as_descriptor(token), token)

        msg = n1.broadcast_mutation("KHASRA-402", {"area_sqm": 1200.0})

        # Deliver message first time -> accepted
        assert n2.receive_gossip_message(msg) is True

        # Deliver same message again (loop from alternate route) -> dropped
        assert n2.receive_gossip_message(msg) is False

    def test_multi_hop_gossip_relay(self, surveyor_nodes: List[P2PMeshNode]):
        # Topology: N1 <---> N2 <---> N3 (Linear mesh: N1 not directly connected to N3)
        n1, n2, n3 = surveyor_nodes[0], surveyor_nodes[1], surveyor_nodes[2]
        token = "token-mesh"
        n1.connect_peer(n2.as_descriptor(token), token)
        n2.connect_peer(n1.as_descriptor(token), token)
        n2.connect_peer(n3.as_descriptor(token), token)
        n3.connect_peer(n2.as_descriptor(token), token)

        # N1 generates survey update
        msg = n1.broadcast_mutation("KHASRA-501", {"boundary": "verified_by_n1"})

        # N2 receives and forwards to peers (excluding sender N1)
        relayed = n2.receive_and_forward(msg)
        assert len(relayed) == 1
        forwarded_to_n3 = relayed[0]
        assert forwarded_to_n3.hops == 1

        # N3 receives relayed message
        assert n3.receive_gossip_message(forwarded_to_n3) is True
        assert "KHASRA-501" in n3.get_local_store()
        assert n3.get_local_store()["KHASRA-501"]["boundary"] == "verified_by_n1"

    def test_hop_limit_prevents_infinite_propagation(
        self, surveyor_nodes: List[P2PMeshNode]
    ):
        n1, n2 = surveyor_nodes[0], surveyor_nodes[1]
        msg = n1.broadcast_mutation("KHASRA-999", {"val": 1})
        msg.hops = 10  # Exceeds max allowable hop count (e.g. 5)

        # Receiver drops message due to TTL/hop ceiling
        accepted = n2.receive_gossip_message(msg, max_hops=5)
        assert accepted is False
        assert "KHASRA-999" not in n2.get_local_store()


# =====================================================================
# 4. Anti-Entropy Pairwise Synchronization Tests
# =====================================================================

class TestAntiEntropySync:
    """Tests for pairwise synchronization exchange reconciling missed mutations."""

    def test_pairwise_anti_entropy_reconciles_divergent_stores(
        self, surveyor_nodes: List[P2PMeshNode]
    ):
        n1, n2 = surveyor_nodes[0], surveyor_nodes[1]
        token = "secret"
        n1.connect_peer(n2.as_descriptor(token), token)
        n2.connect_peer(n1.as_descriptor(token), token)

        # N1 collected parcels while isolated
        n1.store_local("P101", {"owner": "Farmer A", "area": 1000}, version=1)
        n1.store_local("P102", {"owner": "Farmer B", "area": 2000}, version=1)

        # N2 collected parcel P201 while isolated
        n2.store_local("P201", {"owner": "Farmer C", "area": 1500}, version=1)

        # Perform anti-entropy sync exchange between N1 and N2
        summary = n1.perform_anti_entropy_sync(n2)
        assert summary["success"] is True
        assert summary["items_sent_to_peer"] == 2
        assert summary["items_received_from_peer"] == 1

        # Both nodes now hold union of all parcels
        assert "P101" in n2.get_local_store()
        assert "P102" in n2.get_local_store()
        assert "P201" in n1.get_local_store()


# =====================================================================
# 5. P2PMeshCoordinator End-to-End Cluster Tests
# =====================================================================

class TestP2PMeshCoordinator:
    """Tests for multi-node mesh coordinator managing cluster lifecycle and statistics."""

    def test_coordinator_manages_mesh_and_delivers_cluster_status(
        self, surveyor_nodes: List[P2PMeshNode]
    ):
        coordinator = P2PMeshCoordinator()
        for node in surveyor_nodes:
            coordinator.register_node(node)

        # Form full mesh
        coordinator.auto_mesh(shared_secret="auto-mesh-secret")

        stats = coordinator.get_mesh_stats()
        assert stats["total_nodes"] == 3
        assert stats["total_active_links"] >= 2
        assert stats["partitioned"] is False

    def test_mesh_partition_detection_and_healing(
        self, surveyor_nodes: List[P2PMeshNode]
    ):
        coordinator = P2PMeshCoordinator()
        n1, n2, n3 = surveyor_nodes[0], surveyor_nodes[1], surveyor_nodes[2]
        coordinator.register_node(n1)
        coordinator.register_node(n2)
        coordinator.register_node(n3)

        # Connect n1-n2 and isolate n3
        token = "mesh-token"
        n1.connect_peer(n2.as_descriptor(token), token)
        n2.connect_peer(n1.as_descriptor(token), token)

        stats = coordinator.get_mesh_stats()
        assert stats["isolated_nodes_count"] == 1

        # Heal network by connecting n2-n3
        n2.connect_peer(n3.as_descriptor(token), token)
        n3.connect_peer(n2.as_descriptor(token), token)

        stats_healed = coordinator.get_mesh_stats()
        assert stats_healed["isolated_nodes_count"] == 0
