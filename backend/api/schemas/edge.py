"""Edge worker and heartbeat monitoring API schemas."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class HardwareMetricsSchema(BaseModel):
    """Hardware health and performance telemetry reported by an edge worker."""

    cpu_usage_pct: float = Field(..., ge=0.0, le=100.0, description="CPU utilization percentage")
    memory_usage_pct: float = Field(..., ge=0.0, le=100.0, description="RAM utilization percentage")
    memory_available_mb: float = Field(..., ge=0.0, description="Available RAM in megabytes")
    disk_free_gb: float = Field(..., ge=0.0, description="Free disk space in gigabytes")
    active_tasks: int = Field(0, ge=0, description="Number of currently executing tasks")
    completed_tasks: int = Field(0, ge=0, description="Total completed tasks since startup")
    uptime_seconds: float = Field(0.0, ge=0.0, description="Worker process uptime in seconds")


class EdgeWorkerRegisterRequest(BaseModel):
    """Payload to register a new edge compute node with the central coordinator."""

    name: str = Field(..., min_length=1, description="Human-readable worker name")
    district: str = Field(..., min_length=1, description="Revenue district jurisdiction")
    state: str = Field(..., min_length=1, description="State name")
    ip_address: str = Field(..., min_length=1, description="Network IP address of edge node")
    port: int = Field(..., ge=1, le=65535, description="Network service port")
    capabilities: List[str] = Field(
        default_factory=list,
        description="Supported processing capabilities (e.g. CADASTRAL_INGESTION, TOPOLOGY_CLEANING)",
    )
    tags: Optional[Dict[str, str]] = Field(default=None, description="Arbitrary metadata key-value tags")
    heartbeat_timeout_sec: float = Field(30.0, gt=0.0, description="Heartbeat timeout threshold in seconds")
    worker_id: Optional[str] = Field(None, description="Optional custom unique worker identifier")


class EdgeWorkerResponse(BaseModel):
    """Edge worker node summary and connection attributes."""

    worker_id: str = Field(..., description="Unique alphanumeric worker ID")
    name: str = Field(..., description="Worker name")
    district: str = Field(..., description="District jurisdiction")
    state: str = Field(..., description="State jurisdiction")
    ip_address: str = Field(..., description="IP address")
    port: int = Field(..., description="Port")
    capabilities: List[str] = Field(..., description="List of advertised capability tokens")
    status: str = Field(..., description="Current lifecycle state (ONLINE, BUSY, IDLE, DEGRADED, OFFLINE)")
    registered_at: str = Field(..., description="ISO 8601 registration timestamp")
    last_heartbeat_at: str = Field(..., description="ISO 8601 last heartbeat timestamp")
    heartbeat_timeout_sec: float = Field(..., description="Timeout interval in seconds")
    metrics: Optional[Dict[str, Any]] = Field(None, description="Most recent hardware telemetry")
    tags: Dict[str, str] = Field(default_factory=dict, description="Metadata tags")
    consecutive_missed_heartbeats: int = Field(0, description="Count of missed heartbeats")
    auth_token: str = Field(..., description="Worker authentication lease token")


class EdgeWorkerHeartbeatRequest(BaseModel):
    """Periodic heartbeat transmission payload from an edge worker."""

    status: Optional[str] = Field("ONLINE", description="Reported operational status")
    metrics: Optional[HardwareMetricsSchema] = Field(None, description="Hardware telemetry snapshot")
    active_task_ids: Optional[List[str]] = Field(default_factory=list, description="IDs of currently executing tasks")


class EdgeWorkerHeartbeatResponse(BaseModel):
    """Coordinator acknowledgment of worker heartbeat transmission."""

    success: bool = True
    worker_id: str = Field(..., description="Worker identifier")
    status: str = Field(..., description="Registered status after heartbeat update")
    last_heartbeat_at: str = Field(..., description="ISO 8601 heartbeat timestamp")
    consecutive_missed_heartbeats: int = Field(0, description="Reset missed heartbeat counter")


class ClusterHealthSummaryResponse(BaseModel):
    """Aggregate cluster health, capacity, and active load summary."""

    total_workers: int = Field(..., description="Total registered workers")
    online_count: int = Field(..., description="Workers in ONLINE status")
    idle_count: int = Field(..., description="Workers in IDLE status")
    busy_count: int = Field(..., description="Workers in BUSY status")
    degraded_count: int = Field(..., description="Workers in DEGRADED status")
    offline_count: int = Field(..., description="Workers in OFFLINE status")
    active_workers_count: int = Field(..., description="Total responsive workers (ONLINE + IDLE + BUSY)")
    average_cpu_pct: float = Field(..., description="Average CPU utilization across active workers")
    average_memory_pct: float = Field(..., description="Average RAM utilization across active workers")
    total_active_tasks: int = Field(..., description="Total tasks currently executing across cluster")
    total_completed_tasks: int = Field(..., description="Cumulative completed tasks")


class DeregisterWorkerResponse(BaseModel):
    """Response returned upon worker deregistration."""

    success: bool
    worker_id: str
    message: str


class SyncQueueItemSchema(BaseModel):
    """Single cadastral mutation item submitted in an offline sync batch."""

    operation_id: str = Field(..., description="Unique UUID for mutation operation")
    worker_id: str = Field(..., description="Worker node ID originating mutation")
    entity_type: str = Field("PARCEL", description="Entity type, e.g. PARCEL")
    entity_id: str = Field(..., description="Target entity ID, e.g. KHASRA-101")
    operation_type: str = Field("UPDATE_PARCEL", description="Mutation type")
    sequence_number: int = Field(..., ge=1, description="Monotonic sequence number on worker queue")
    client_timestamp: str = Field(..., description="ISO 8601 timestamp when mutation was recorded")
    base_version: int = Field(..., ge=0, description="Version of entity on worker when edit started")
    payload: Dict[str, Any] = Field(..., description="Mutation payload")
    checksum: str = Field(..., description="SHA-256 checksum of payload")
    idempotency_key: str = Field(..., description="Idempotency key")
    status: Optional[str] = Field("QUEUED", description="Queue status")


class SyncBatchSubmitRequest(BaseModel):
    """Batch of offline mutations submitted by an edge worker."""

    worker_id: str = Field(..., description="Originating edge worker ID")
    items: List[SyncQueueItemSchema] = Field(..., min_length=1, description="List of queued sync items")
    policy: Optional[str] = Field(
        None,
        description="Optional conflict resolution policy override (LAST_WRITE_WINS, COORDINATOR_WINS, EDGE_WINS, MANUAL_DISPUTE)",
    )


class ConflictRecordSchema(BaseModel):
    """Audit schema for a resolved or escalated conflict."""

    conflict_id: str
    entity_id: str
    operation_id: str
    worker_id: str
    client_version: int
    server_version: int
    policy_applied: str
    resolved_version: int
    is_disputed: bool
    details: str
    resolved_at: str


class SyncBatchSubmitResponse(BaseModel):
    """Coordinator acknowledgment and conflict report for submitted sync batch."""

    batch_id: str
    total_items: int
    committed_count: int
    conflict_count: int
    failed_count: int
    acknowledged_operation_ids: List[str]
    conflicts: List[ConflictRecordSchema]


class EntityVersionResponse(BaseModel):
    """Current server version and state for an authoritative entity."""

    entity_id: str
    version: int
    updated_at: str
    is_disputed: bool
    payload: Dict[str, Any]


class ResolveDisputeRequest(BaseModel):
    """Payload to manually arbitrate and resolve a disputed parcel."""

    resolved_payload: Dict[str, Any] = Field(..., description="Final authoritative cadastral payload")
    resolution_method: str = Field("TEHSILDAR_SIGN_OFF", description="Administrative resolution method")
    resolved_by: str = Field(..., min_length=1, description="Officer ID or designation arbitrating dispute")


class ResolveDisputeResponse(BaseModel):
    """Response returned upon resolving a disputed cadastral entity."""

    success: bool = True
    entity_id: str
    new_version: int
    is_disputed: bool
    resolved_by: str
    resolution_method: str
    updated_at: str


class DeltaChunkSchema(BaseModel):
    """Payload schema for an individual delta transmission chunk."""

    delta_id: str = Field(..., description="Unique identifier for the delta session")
    chunk_index: int = Field(..., ge=0, description="0-indexed sequence position of the chunk")
    total_chunks: int = Field(..., ge=1, description="Total expected chunks for complete payload")
    chunk_data_b64: str = Field(..., description="Base64-encoded binary chunk content")
    checksum: str = Field(..., description="SHA-256 hex digest of the chunk binary data")


class DeltaTransmissionSubmitRequest(BaseModel):
    """Request payload containing a chunked compressed delta from an edge worker."""

    worker_id: str = Field(..., description="Edge worker originating the transmission")
    delta_id: str = Field(..., description="Session identifier for the delta")
    compression: str = Field("GZIP", description="Compression codec used (GZIP, ZLIB, NONE)")
    network_tier: Optional[str] = Field("3G", description="Network tier for transmission profile (2G, 3G, 4G)")
    chunks: List[DeltaChunkSchema] = Field(..., min_length=1, description="List of delta chunks")


class DeltaTransmissionSubmitResponse(BaseModel):
    """Coordinator acknowledgment and application report for submitted delta transmission."""

    success: bool = True
    delta_id: str
    total_chunks_received: int
    uncompressed_bytes: int
    compressed_bytes: int
    compression_ratio: float
    total_entities_affected: int
    added_count: int
    modified_count: int
    deleted_count: int
    applied_at: str


class PeerConnectRequest(BaseModel):
    """Payload to establish a peer link with another mesh node."""

    node_id: str = Field(..., description="Target peer node identifier")
    node_name: str = Field(..., description="Target peer node name")
    ip_address: str = Field(..., description="Peer IP address")
    port: int = Field(..., ge=1, le=65535, description="Peer port")
    district: str = Field(..., description="Peer district jurisdiction")
    shared_secret: str = Field(..., description="Authentication lease secret for mutual validation")


class PeerDescriptorResponse(BaseModel):
    """Descriptor summary of a connected or discovered mesh peer."""

    node_id: str
    node_name: str
    ip_address: str
    port: int
    district: str
    link_state: str
    last_contact_at: Optional[str] = None


class GossipBroadcastRequest(BaseModel):
    """Payload to broadcast a local cadastral mutation across the mesh."""

    entity_id: str = Field(..., description="Target parcel/cadastre entity ID")
    payload: Dict[str, Any] = Field(..., description="Cadastral mutation payload")


class GossipMessageResponse(BaseModel):
    """Epidemic gossip message packet format."""

    msg_id: str
    origin_node_id: str
    message_type: str
    entity_id: str
    payload: Dict[str, Any]
    vector_clock: Dict[str, int]
    timestamp: str
    hops: int
    checksum: str


class MeshStatsResponse(BaseModel):
    """Aggregate mesh topology and partition statistics."""

    total_nodes: int
    total_active_links: int
    isolated_nodes_count: int
    partitioned: bool




