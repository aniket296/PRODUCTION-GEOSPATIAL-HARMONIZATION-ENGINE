"""Edge worker registration and heartbeat monitoring API endpoints."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, status

from backend.api.schemas.edge import (
    ClusterHealthSummaryResponse,
    ConflictRecordSchema,
    DeltaChunkSchema,
    DeltaTransmissionSubmitRequest,
    DeltaTransmissionSubmitResponse,
    DeregisterWorkerResponse,
    EdgeWorkerHeartbeatRequest,
    EdgeWorkerHeartbeatResponse,
    EdgeWorkerRegisterRequest,
    EdgeWorkerResponse,
    EntityVersionResponse,
    GossipBroadcastRequest,
    GossipMessageResponse,
    HardwareMetricsSchema,
    MeshStatsResponse,
    PeerConnectRequest,
    PeerDescriptorResponse,
    ResolveDisputeRequest,
    ResolveDisputeResponse,
    SyncBatchSubmitRequest,
    SyncBatchSubmitResponse,
    SyncQueueItemSchema,
)
from backend.edge import (
    BandwidthThrottler,
    CadastralDelta,
    ConflictResolutionPolicy,
    DeltaChunk,
    DeltaChunkManager,
    DeltaCompressionType,
    DeltaCompressor,
    DeltaSyncEngine,
    EdgeWorker,
    HardwareMetrics,
    HeartbeatPayload,
    MeshLinkState,
    MeshNodeStatus,
    NetworkTier,
    P2PMeshNode,
    PeerDescriptor,
    SyncError,
    SyncItemStatus,
    SyncOperationType,
    SyncQueueItem,
    WorkerCapability,
    WorkerNotFoundError,
    WorkerRegistrationError,
    WorkerStatus,
    edge_worker_registry,
    heartbeat_monitor,
    mesh_coordinator,
    sync_coordinator,
)
import base64
from datetime import datetime, timezone





router = APIRouter(prefix="/edge", tags=["edge"])


def _worker_to_response(w: EdgeWorker) -> EdgeWorkerResponse:
    return EdgeWorkerResponse(
        worker_id=w.worker_id,
        name=w.name,
        district=w.district,
        state=w.state,
        ip_address=w.ip_address,
        port=w.port,
        capabilities=[c.value for c in w.capabilities],
        status=w.status.value,
        registered_at=w.registered_at.isoformat(),
        last_heartbeat_at=w.last_heartbeat_at.isoformat(),
        heartbeat_timeout_sec=w.heartbeat_timeout_sec,
        metrics=w.metrics.to_dict() if w.metrics else None,
        tags=dict(w.tags),
        consecutive_missed_heartbeats=w.consecutive_missed_heartbeats,
        auth_token=w.auth_token,
    )


@router.post("/workers/register", response_model=EdgeWorkerResponse, status_code=status.HTTP_201_CREATED)
def register_edge_worker(request: EdgeWorkerRegisterRequest) -> EdgeWorkerResponse:
    """Register a new decentralized edge compute node or update an active registration lease."""
    caps: List[WorkerCapability] = []
    for cap_str in request.capabilities:
        try:
            caps.append(WorkerCapability(cap_str.upper()))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown worker capability '{cap_str}'. Valid values: {[c.value for c in WorkerCapability]}",
            )

    try:
        worker = edge_worker_registry.register_worker(
            name=request.name,
            district=request.district,
            state=request.state,
            ip_address=request.ip_address,
            port=request.port,
            capabilities=caps,
            tags=request.tags,
            heartbeat_timeout_sec=request.heartbeat_timeout_sec,
            worker_id=request.worker_id,
        )
    except WorkerRegistrationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return _worker_to_response(worker)


@router.get("/workers", response_model=List[EdgeWorkerResponse], status_code=status.HTTP_200_OK)
def list_edge_workers(
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by WorkerStatus"),
    district: Optional[str] = Query(None, description="Filter by administrative district"),
    state_filter: Optional[str] = Query(None, alias="state", description="Filter by state"),
    capability: Optional[str] = Query(None, description="Filter by capability"),
) -> List[EdgeWorkerResponse]:
    """List registered edge workers matching optional query criteria."""
    s_enum = None
    if status_filter:
        try:
            s_enum = WorkerStatus(status_filter.upper())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid worker status '{status_filter}'.",
            )

    c_enum = None
    if capability:
        try:
            c_enum = WorkerCapability(capability.upper())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid worker capability '{capability}'.",
            )

    workers = edge_worker_registry.list_workers(
        status=s_enum,
        district=district,
        state=state_filter,
        capability=c_enum,
    )
    return [_worker_to_response(w) for w in workers]


@router.get("/workers/eligible", response_model=List[EdgeWorkerResponse], status_code=status.HTTP_200_OK)
def find_eligible_edge_workers(
    capability: Optional[str] = Query(None, description="Required processing capability"),
    district: Optional[str] = Query(None, description="Target district jurisdiction"),
    max_cpu_pct: float = Query(85.0, ge=0.0, le=100.0, description="Maximum CPU threshold"),
) -> List[EdgeWorkerResponse]:
    """Find active and available edge workers sorted by lowest resource utilization."""
    c_enum = None
    if capability:
        try:
            c_enum = WorkerCapability(capability.upper())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid worker capability '{capability}'.",
            )

    eligible = edge_worker_registry.find_eligible_workers(
        required_capability=c_enum,
        district=district,
        max_cpu_pct=max_cpu_pct,
    )
    return [_worker_to_response(w) for w in eligible]


@router.get("/workers/{worker_id}", response_model=EdgeWorkerResponse, status_code=status.HTTP_200_OK)
def get_edge_worker(worker_id: str) -> EdgeWorkerResponse:
    """Retrieve details for a specific registered edge worker node."""
    worker = edge_worker_registry.get_worker(worker_id)
    if not worker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Edge worker '{worker_id}' is not registered.",
        )
    return _worker_to_response(worker)


@router.post("/workers/{worker_id}/heartbeat", response_model=EdgeWorkerHeartbeatResponse, status_code=status.HTTP_200_OK)
def record_edge_heartbeat(
    worker_id: str,
    request: EdgeWorkerHeartbeatRequest,
) -> EdgeWorkerHeartbeatResponse:
    """Record heartbeat packet and hardware telemetry from an edge worker."""
    s_enum = WorkerStatus.ONLINE
    if request.status:
        try:
            s_enum = WorkerStatus(request.status.upper())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid worker status '{request.status}'.",
            )

    metrics_obj = None
    if request.metrics:
        m = request.metrics
        metrics_obj = HardwareMetrics(
            cpu_usage_pct=m.cpu_usage_pct,
            memory_usage_pct=m.memory_usage_pct,
            memory_available_mb=m.memory_available_mb,
            disk_free_gb=m.disk_free_gb,
            active_tasks=m.active_tasks,
            completed_tasks=m.completed_tasks,
            uptime_seconds=m.uptime_seconds,
        )

    payload = HeartbeatPayload(
        worker_id=worker_id,
        status=s_enum,
        metrics=metrics_obj,
        active_task_ids=request.active_task_ids or [],
    )

    try:
        updated = edge_worker_registry.record_heartbeat(worker_id, payload)
    except WorkerNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Edge worker '{worker_id}' is not registered.",
        )

    return EdgeWorkerHeartbeatResponse(
        success=True,
        worker_id=updated.worker_id,
        status=updated.status.value,
        last_heartbeat_at=updated.last_heartbeat_at.isoformat(),
        consecutive_missed_heartbeats=updated.consecutive_missed_heartbeats,
    )


@router.delete("/workers/{worker_id}", response_model=DeregisterWorkerResponse, status_code=status.HTTP_200_OK)
def deregister_edge_worker(
    worker_id: str,
    reason: str = Query("graceful_shutdown", description="Reason for deregistration"),
) -> DeregisterWorkerResponse:
    """Deregister an edge worker from active cluster catalog."""
    success = edge_worker_registry.deregister_worker(worker_id, reason=reason)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Edge worker '{worker_id}' is not registered.",
        )

    return DeregisterWorkerResponse(
        success=True,
        worker_id=worker_id,
        message=f"Edge worker '{worker_id}' successfully deregistered.",
    )


@router.get("/health", response_model=ClusterHealthSummaryResponse, status_code=status.HTTP_200_OK)
def get_cluster_health() -> ClusterHealthSummaryResponse:
    """Retrieve real-time cluster health summary and resource utilization across edge workers."""
    # Run liveness sweep to update statuses if heartbeats have expired
    heartbeat_monitor.check_liveness()
    summary = heartbeat_monitor.get_cluster_health_summary()
    return ClusterHealthSummaryResponse(**summary)


# ---------------------------------------------------------------------------
# Offline Synchronization & Conflict Resolution Endpoints
# ---------------------------------------------------------------------------

@router.post("/sync/batch", response_model=SyncBatchSubmitResponse, status_code=status.HTTP_200_OK)
def submit_offline_sync_batch(request: SyncBatchSubmitRequest) -> SyncBatchSubmitResponse:
    """Submit a batch of queued offline cadastral mutations for central reconciliation."""
    policy_enum = None
    if request.policy:
        try:
            policy_enum = ConflictResolutionPolicy(request.policy.upper())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid conflict resolution policy '{request.policy}'. Valid policies: {[p.value for p in ConflictResolutionPolicy]}",
            )

    domain_items: List[SyncQueueItem] = []
    for item in request.items:
        try:
            op_enum = SyncOperationType(item.operation_type.upper())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid sync operation type '{item.operation_type}'.",
            )

        try:
            ts = datetime.fromisoformat(item.client_timestamp)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid ISO 8601 client_timestamp '{item.client_timestamp}'.",
            )

        status_enum = SyncItemStatus.QUEUED
        if item.status:
            try:
                status_enum = SyncItemStatus(item.status.upper())
            except ValueError:
                status_enum = SyncItemStatus.QUEUED

        domain_item = SyncQueueItem(
            operation_id=item.operation_id,
            worker_id=item.worker_id,
            entity_type=item.entity_type,
            entity_id=item.entity_id,
            operation_type=op_enum,
            sequence_number=item.sequence_number,
            client_timestamp=ts,
            base_version=item.base_version,
            payload=item.payload,
            checksum=item.checksum,
            idempotency_key=item.idempotency_key,
            status=status_enum,
        )
        domain_items.append(domain_item)

    batch_result = sync_coordinator.reconcile_batch(domain_items, policy=policy_enum)

    conflict_schemas: List[ConflictRecordSchema] = []
    for c in batch_result.conflicts:
        conflict_schemas.append(
            ConflictRecordSchema(
                conflict_id=c.conflict_id,
                entity_id=c.entity_id,
                operation_id=c.operation_id,
                worker_id=c.worker_id,
                client_version=c.client_version,
                server_version=c.server_version,
                policy_applied=c.policy_applied.value,
                resolved_version=c.resolved_version,
                is_disputed=c.is_disputed,
                details=c.details,
                resolved_at=c.resolved_at.isoformat(),
            )
        )

    return SyncBatchSubmitResponse(
        batch_id=batch_result.batch_id,
        total_items=batch_result.total_items,
        committed_count=batch_result.committed_count,
        conflict_count=batch_result.conflict_count,
        failed_count=batch_result.failed_count,
        acknowledged_operation_ids=batch_result.acknowledged_operation_ids,
        conflicts=conflict_schemas,
    )


@router.get("/sync/entity/{entity_id}", response_model=EntityVersionResponse, status_code=status.HTTP_200_OK)
def get_entity_version(entity_id: str) -> EntityVersionResponse:
    """Retrieve the current authoritative state and version for a cadastral entity."""
    record = sync_coordinator.get_server_entity(entity_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Authoritative entity '{entity_id}' not found in coordinator registry.",
        )

    return EntityVersionResponse(
        entity_id=record.entity_id,
        version=record.version,
        updated_at=record.updated_at.isoformat(),
        is_disputed=record.is_disputed,
        payload=record.payload,
    )


@router.post("/sync/disputes/{entity_id}/resolve", response_model=ResolveDisputeResponse, status_code=status.HTTP_200_OK)
def resolve_disputed_entity(entity_id: str, request: ResolveDisputeRequest) -> ResolveDisputeResponse:
    """Manually resolve an escalated dispute on a cadastral entity."""
    try:
        updated_record = sync_coordinator.resolve_manual_dispute(
            entity_id=entity_id,
            resolved_payload=request.resolved_payload,
            resolution_method=request.resolution_method,
            resolved_by=request.resolved_by,
        )
    except SyncError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    return ResolveDisputeResponse(
        success=True,
        entity_id=updated_record.entity_id,
        new_version=updated_record.version,
        is_disputed=updated_record.is_disputed,
        resolved_by=request.resolved_by,
        resolution_method=request.resolution_method,
        updated_at=updated_record.updated_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Compressed Delta Transmission Endpoints
# ---------------------------------------------------------------------------

@router.post("/delta/transmit", response_model=DeltaTransmissionSubmitResponse, status_code=status.HTTP_200_OK)
def submit_delta_transmission(request: DeltaTransmissionSubmitRequest) -> DeltaTransmissionSubmitResponse:
    """Submit chunked, compressed cadastral delta for central reassembly and application."""
    # 1. Parse compression type
    try:
        comp_type = DeltaCompressionType(request.compression.upper())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported compression codec '{request.compression}'. Valid options: {[c.value for c in DeltaCompressionType]}",
        )

    # 2. Parse and validate chunks
    domain_chunks: List[DeltaChunk] = []
    for ch_schema in request.chunks:
        try:
            chunk_raw = base64.b64decode(ch_schema.chunk_data_b64)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid base64 encoding in chunk index {ch_schema.chunk_index}.",
            )

        chunk_obj = DeltaChunk(
            delta_id=ch_schema.delta_id,
            chunk_index=ch_schema.chunk_index,
            total_chunks=ch_schema.total_chunks,
            chunk_data=chunk_raw,
            checksum=ch_schema.checksum,
        )

        if not chunk_obj.verify_checksum():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Checksum verification failed for chunk index {ch_schema.chunk_index}.",
            )
        domain_chunks.append(chunk_obj)

    # 3. Reassemble chunks
    chunk_manager = DeltaChunkManager()
    try:
        compressed_bytes = chunk_manager.reassemble(domain_chunks)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # 4. Decompress delta
    compressor = DeltaCompressor(compression_type=comp_type)
    try:
        delta = compressor.decompress(compressed_bytes)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to decompress delta payload: {e}",
        )

    # 5. Apply delta mutations directly to central store
    for add_rec in delta.added:
        if add_rec.new_data:
            sync_coordinator.set_server_entity(
                entity_id=add_rec.entity_id,
                version=1,
                payload=add_rec.new_data,
            )

    for mod_rec in delta.modified:
        if mod_rec.new_data:
            existing = sync_coordinator.get_server_entity(mod_rec.entity_id)
            current_v = existing.version if existing else 1
            sync_coordinator.set_server_entity(
                entity_id=mod_rec.entity_id,
                version=current_v + 1,
                payload=mod_rec.new_data,
            )

    for del_rec in delta.deleted:
        with sync_coordinator._lock:
            sync_coordinator._server_store.pop(del_rec.entity_id, None)

    orig_size = compressor.get_last_stats().get("original_bytes", len(compressed_bytes))
    ratio = (orig_size / len(compressed_bytes)) if len(compressed_bytes) > 0 else 1.0

    return DeltaTransmissionSubmitResponse(
        success=True,
        delta_id=delta.delta_id,
        total_chunks_received=len(domain_chunks),
        uncompressed_bytes=orig_size,
        compressed_bytes=len(compressed_bytes),
        compression_ratio=ratio,
        total_entities_affected=delta.total_changes,
        added_count=len(delta.added),
        modified_count=len(delta.modified),
        deleted_count=len(delta.deleted),
        applied_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# P2P Mesh Synchronization Endpoints
# ---------------------------------------------------------------------------

@router.get("/mesh/stats", response_model=MeshStatsResponse, status_code=status.HTTP_200_OK)
def get_mesh_statistics() -> MeshStatsResponse:
    """Retrieve ad-hoc P2P mesh topology density and partition metrics."""
    stats = mesh_coordinator.get_mesh_stats()
    return MeshStatsResponse(**stats)


@router.post("/mesh/nodes/{node_id}/connect-peer", response_model=PeerDescriptorResponse, status_code=status.HTTP_200_OK)
def connect_mesh_peer(node_id: str, request: PeerConnectRequest) -> PeerDescriptorResponse:
    """Connect a local mesh node to a remote peer via authenticated shared secret."""
    node = mesh_coordinator.get_node(node_id)
    if not node:
        # Auto-create if node exists in edge worker registry
        worker = edge_worker_registry.get_worker(node_id)
        if not worker:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Mesh node '{node_id}' is not registered.",
            )
        node = P2PMeshNode(
            node_id=worker.worker_id,
            node_name=worker.name,
            district=worker.district,
            ip_address=worker.ip_address,
            port=worker.port,
        )
        mesh_coordinator.register_node(node)

    desc = PeerDescriptor(
        node_id=request.node_id,
        node_name=request.node_name,
        ip_address=request.ip_address,
        port=request.port,
        district=request.district,
        shared_secret=request.shared_secret,
    )

    success = node.connect_peer(desc, auth_secret=request.shared_secret)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Mutual peer authentication failed for target peer '{request.node_id}'.",
        )

    updated_peer = node.get_peer(request.node_id)
    return PeerDescriptorResponse(
        node_id=updated_peer.node_id,
        node_name=updated_peer.node_name,
        ip_address=updated_peer.ip_address,
        port=updated_peer.port,
        district=updated_peer.district,
        link_state=updated_peer.link_state.value,
        last_contact_at=updated_peer.last_contact_at.isoformat() if updated_peer.last_contact_at else None,
    )


@router.get("/mesh/nodes/{node_id}/peers", response_model=List[PeerDescriptorResponse], status_code=status.HTTP_200_OK)
def list_mesh_peers(node_id: str) -> List[PeerDescriptorResponse]:
    """List peer connections and link states for a specific mesh node."""
    node = mesh_coordinator.get_node(node_id)
    if not node:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Mesh node '{node_id}' is not registered.",
        )

    return [
        PeerDescriptorResponse(
            node_id=p.node_id,
            node_name=p.node_name,
            ip_address=p.ip_address,
            port=p.port,
            district=p.district,
            link_state=p.link_state.value,
            last_contact_at=p.last_contact_at.isoformat() if p.last_contact_at else None,
        )
        for p in node.list_peers()
    ]


@router.post("/mesh/nodes/{node_id}/gossip/broadcast", response_model=GossipMessageResponse, status_code=status.HTTP_200_OK)
def broadcast_gossip_mutation(node_id: str, request: GossipBroadcastRequest) -> GossipMessageResponse:
    """Broadcast local cadastral mutation across connected mesh peers via epidemic gossip."""
    node = mesh_coordinator.get_node(node_id)
    if not node:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Mesh node '{node_id}' is not registered.",
        )

    msg = node.broadcast_mutation(
        entity_id=request.entity_id,
        payload=request.payload,
    )

    return GossipMessageResponse(
        msg_id=msg.msg_id,
        origin_node_id=msg.origin_node_id,
        message_type=msg.message_type.value,
        entity_id=msg.entity_id,
        payload=msg.payload,
        vector_clock=msg.vector_clock,
        timestamp=msg.timestamp.isoformat(),
        hops=msg.hops,
        checksum=msg.checksum,
    )




