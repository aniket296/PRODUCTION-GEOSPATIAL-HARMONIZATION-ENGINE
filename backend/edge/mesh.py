"""Peer-to-peer (P2P) ad-hoc synchronization mesh for decentralized edge nodes.

Enables:
1. Distributed vector clocks for causal ordering across field nodes.
2. Ad-hoc peer descriptor management, mutual token authentication, and link states.
3. Multi-hop epidemic gossip propagation with message deduplication and TTL/hop limits.
4. Pairwise anti-entropy background synchronization for reconciling divergent local stores.
5. Mesh cluster coordinator for topology inspection and partition tracking.
"""

from __future__ import annotations

import copy
import enum
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


class MeshLinkState(str, enum.Enum):
    """Lifecycle connection status of a peer-to-peer link."""

    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    FAILED = "FAILED"


class MeshNodeStatus(str, enum.Enum):
    """Operational status of a mesh node."""

    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    ISOLATED = "ISOLATED"


class GossipMessageType(str, enum.Enum):
    """Types of gossip messages propagated across the mesh."""

    CADASTRAL_MUTATION = "CADASTRAL_MUTATION"
    ANTI_ENTROPY_REQUEST = "ANTI_ENTROPY_REQUEST"
    ANTI_ENTROPY_RESPONSE = "ANTI_ENTROPY_RESPONSE"
    HEARTBEAT_PING = "HEARTBEAT_PING"


@dataclass
class VectorClock:
    """Logical vector clock tracking causal history across mesh nodes."""

    clocks: Dict[str, int] = field(default_factory=dict)

    def get_clock(self, node_id: str) -> int:
        return self.clocks.get(node_id, 0)

    def increment(self, node_id: str) -> int:
        new_val = self.clocks.get(node_id, 0) + 1
        self.clocks[node_id] = new_val
        return new_val

    def merge(self, other: VectorClock) -> VectorClock:
        """Return a new VectorClock with elementwise maximum of both clocks."""
        all_keys = set(self.clocks.keys()) | set(other.clocks.keys())
        merged = {}
        for k in all_keys:
            merged[k] = max(self.clocks.get(k, 0), other.clocks.get(k, 0))
        return VectorClock(merged)

    def is_dominated_by(self, other: VectorClock) -> bool:
        """Return True if self is causally prior to or dominated by other."""
        all_keys = set(self.clocks.keys()) | set(other.clocks.keys())
        at_least_one_smaller = False
        for k in all_keys:
            v_self = self.clocks.get(k, 0)
            v_other = other.clocks.get(k, 0)
            if v_self > v_other:
                return False
            if v_self < v_other:
                at_least_one_smaller = True
        return at_least_one_smaller

    def is_concurrent_with(self, other: VectorClock) -> bool:
        """Return True if neither vector clock dominates the other."""
        return not self.is_dominated_by(other) and not other.is_dominated_by(self) and self.clocks != other.clocks

    def to_dict(self) -> Dict[str, int]:
        return dict(self.clocks)

    @classmethod
    def from_dict(cls, d: Dict[str, int]) -> VectorClock:
        return cls({k: int(v) for k, v in d.items()})


@dataclass
class PeerDescriptor:
    """Connection attributes and security token for a remote mesh peer."""

    node_id: str
    node_name: str
    ip_address: str
    port: int
    district: str
    shared_secret: str
    link_state: MeshLinkState = MeshLinkState.DISCONNECTED
    last_contact_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_name": self.node_name,
            "ip_address": self.ip_address,
            "port": self.port,
            "district": self.district,
            "link_state": self.link_state.value,
            "last_contact_at": self.last_contact_at.isoformat() if self.last_contact_at else None,
        }


@dataclass
class GossipMessage:
    """Encapsulated message propagated through the peer-to-peer epidemic gossip mesh."""

    msg_id: str
    origin_node_id: str
    message_type: GossipMessageType
    entity_id: str
    payload: Dict[str, Any]
    vector_clock: Dict[str, int]
    timestamp: datetime
    hops: int = 0
    checksum: str = ""

    @classmethod
    def create(
        cls,
        origin_node_id: str,
        entity_id: str,
        payload: Dict[str, Any],
        vector_clock: Dict[str, int],
        message_type: GossipMessageType = GossipMessageType.CADASTRAL_MUTATION,
        hops: int = 0,
    ) -> GossipMessage:
        mid = str(uuid.uuid4())
        ts = datetime.now(timezone.utc)
        ck_source = f"{mid}:{origin_node_id}:{entity_id}:{hops}"
        ck = hashlib.sha256(ck_source.encode("utf-8")).hexdigest()
        return cls(
            msg_id=mid,
            origin_node_id=origin_node_id,
            message_type=message_type,
            entity_id=entity_id,
            payload=copy.deepcopy(payload),
            vector_clock=dict(vector_clock),
            timestamp=ts,
            hops=hops,
            checksum=ck,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "origin_node_id": self.origin_node_id,
            "message_type": self.message_type.value,
            "entity_id": self.entity_id,
            "payload": self.payload,
            "vector_clock": self.vector_clock,
            "timestamp": self.timestamp.isoformat(),
            "hops": self.hops,
            "checksum": self.checksum,
        }


class P2PMeshNode:
    """Individual decentralized peer in the cadastral harmonization mesh."""

    def __init__(
        self,
        node_id: str,
        node_name: str,
        district: str,
        ip_address: str,
        port: int,
        status: MeshNodeStatus = MeshNodeStatus.ONLINE,
    ) -> None:
        self.node_id = node_id
        self.node_name = node_name
        self.district = district
        self.ip_address = ip_address
        self.port = port
        self.status = status

        self.vector_clock = VectorClock({self.node_id: 0})
        self._peers: Dict[str, PeerDescriptor] = {}
        self._seen_message_ids: Set[str] = set()
        self._local_store: Dict[str, Dict[str, Any]] = {}
        self._entity_versions: Dict[str, int] = {}
        self._lock = threading.RLock()

    def as_descriptor(self, shared_secret: str) -> PeerDescriptor:
        return PeerDescriptor(
            node_id=self.node_id,
            node_name=self.node_name,
            ip_address=self.ip_address,
            port=self.port,
            district=self.district,
            shared_secret=shared_secret,
        )

    def list_peers(self) -> List[PeerDescriptor]:
        with self._lock:
            return list(self._peers.values())

    def get_peer(self, peer_id: str) -> Optional[PeerDescriptor]:
        with self._lock:
            return self._peers.get(peer_id)

    def connect_peer(self, descriptor: PeerDescriptor, auth_secret: str) -> bool:
        """Establish direct authenticated connection with a peer."""
        with self._lock:
            if descriptor.shared_secret != auth_secret:
                # Authentication failure
                desc = copy.deepcopy(descriptor)
                desc.link_state = MeshLinkState.DISCONNECTED
                self._peers[descriptor.node_id] = desc
                return False

            desc = copy.deepcopy(descriptor)
            desc.link_state = MeshLinkState.CONNECTED
            desc.last_contact_at = datetime.now(timezone.utc)
            self._peers[descriptor.node_id] = desc
            return True

    def disconnect_peer(self, peer_id: str, reason: str = "User disconnect") -> bool:
        with self._lock:
            peer = self._peers.get(peer_id)
            if not peer:
                return False
            peer.link_state = MeshLinkState.DISCONNECTED
            return True

    def get_seen_message_ids(self) -> Set[str]:
        with self._lock:
            return set(self._seen_message_ids)

    def get_local_store(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._local_store)

    def store_local(self, entity_id: str, data: Dict[str, Any], version: int = 1) -> None:
        """Directly record or update entity in local store."""
        with self._lock:
            self._local_store[entity_id] = copy.deepcopy(data)
            self._entity_versions[entity_id] = version
            self.vector_clock.increment(self.node_id)

    def broadcast_mutation(self, entity_id: str, payload: Dict[str, Any]) -> GossipMessage:
        """Create a local cadastral update and construct a gossip message for dissemination."""
        with self._lock:
            self.vector_clock.increment(self.node_id)
            self._local_store[entity_id] = copy.deepcopy(payload)
            self._entity_versions[entity_id] = self._entity_versions.get(entity_id, 0) + 1

            msg = GossipMessage.create(
                origin_node_id=self.node_id,
                entity_id=entity_id,
                payload=payload,
                vector_clock=self.vector_clock.to_dict(),
                message_type=GossipMessageType.CADASTRAL_MUTATION,
                hops=0,
            )
            self._seen_message_ids.add(msg.msg_id)
            return msg

    def receive_gossip_message(self, msg: GossipMessage, max_hops: int = 5) -> bool:
        """Process incoming gossip message. Returns True if accepted and applied, False if dropped."""
        with self._lock:
            # 1. TTL / Hop limit check
            if msg.hops >= max_hops:
                return False

            # 2. Loop detection & deduplication
            if msg.msg_id in self._seen_message_ids:
                return False

            self._seen_message_ids.add(msg.msg_id)

            # 3. Merge vector clock
            incoming_clock = VectorClock.from_dict(msg.vector_clock)
            self.vector_clock = self.vector_clock.merge(incoming_clock)

            # 4. Apply mutation to local store
            self._local_store[msg.entity_id] = copy.deepcopy(msg.payload)
            self._entity_versions[msg.entity_id] = self._entity_versions.get(msg.entity_id, 0) + 1
            return True

    def receive_and_forward(
        self, msg: GossipMessage, max_hops: int = 5
    ) -> List[GossipMessage]:
        """Receive a message, apply locally, and prepare forward copies for connected peers."""
        with self._lock:
            accepted = self.receive_gossip_message(msg, max_hops=max_hops)
            if not accepted:
                return []

            forward_msgs: List[GossipMessage] = []
            for peer in self._peers.values():
                if peer.link_state == MeshLinkState.CONNECTED and peer.node_id != msg.origin_node_id:
                    f_copy = copy.deepcopy(msg)
                    f_copy.hops += 1
                    forward_msgs.append(f_copy)

            return forward_msgs

    def perform_anti_entropy_sync(self, peer_node: P2PMeshNode) -> Dict[str, Any]:
        """Perform bi-directional anti-entropy sync with another node, reconciling local stores."""
        with self._lock, peer_node._lock:
            # Identify missing or newer keys from self -> peer
            sent_count = 0
            for k, val in self._local_store.items():
                if k not in peer_node._local_store:
                    peer_node._local_store[k] = copy.deepcopy(val)
                    peer_node._entity_versions[k] = self._entity_versions.get(k, 1)
                    sent_count += 1

            # Identify missing or newer keys from peer -> self
            recv_count = 0
            for k, val in peer_node._local_store.items():
                if k not in self._local_store:
                    self._local_store[k] = copy.deepcopy(val)
                    self._entity_versions[k] = peer_node._entity_versions.get(k, 1)
                    recv_count += 1

            # Synchronize vector clocks
            merged_clock = self.vector_clock.merge(peer_node.vector_clock)
            self.vector_clock = copy.deepcopy(merged_clock)
            peer_node.vector_clock = copy.deepcopy(merged_clock)

            return {
                "success": True,
                "items_sent_to_peer": sent_count,
                "items_received_from_peer": recv_count,
                "synced_at": datetime.now(timezone.utc).isoformat(),
            }


class P2PMeshCoordinator:
    """Cluster supervisor managing ad-hoc topology, statistics, and connectivity monitoring."""

    def __init__(self) -> None:
        self._nodes: Dict[str, P2PMeshNode] = {}
        self._lock = threading.RLock()

    def clear(self) -> None:
        """Clear registered mesh nodes."""
        with self._lock:
            self._nodes.clear()

    def register_node(self, node: P2PMeshNode) -> None:
        with self._lock:
            self._nodes[node.node_id] = node

    def get_node(self, node_id: str) -> Optional[P2PMeshNode]:
        with self._lock:
            return self._nodes.get(node_id)

    def list_nodes(self) -> List[P2PMeshNode]:
        with self._lock:
            return list(self._nodes.values())

    def auto_mesh(self, shared_secret: str) -> None:
        """Interconnect all registered nodes into a full mesh topology."""
        with self._lock:
            node_list = list(self._nodes.values())
            for i, n1 in enumerate(node_list):
                for j, n2 in enumerate(node_list):
                    if i != j:
                        n1.connect_peer(n2.as_descriptor(shared_secret), shared_secret)

    def get_mesh_stats(self) -> Dict[str, Any]:
        """Compute mesh health, link density, and partition indicators."""
        with self._lock:
            total_nodes = len(self._nodes)
            active_links = 0
            isolated_count = 0

            for node in self._nodes.values():
                connected_peers = [
                    p for p in node.list_peers() if p.link_state == MeshLinkState.CONNECTED
                ]
                active_links += len(connected_peers)
                if len(connected_peers) == 0:
                    isolated_count += 1

            # Each bidirectional link is counted from both sides
            undirected_links = active_links // 2

            partitioned = isolated_count > 0 or (total_nodes > 1 and undirected_links < total_nodes - 1)

            return {
                "total_nodes": total_nodes,
                "total_active_links": undirected_links,
                "isolated_nodes_count": isolated_count,
                "partitioned": partitioned,
            }


# Global singleton coordinator
mesh_coordinator = P2PMeshCoordinator()
