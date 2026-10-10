"""Edge worker registration and heartbeat monitoring API endpoints."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, status

from backend.api.schemas.edge import (
    ClusterHealthSummaryResponse,
    ConflictRecordSchema,
    DeregisterWorkerResponse,
    EdgeWorkerHeartbeatRequest,
    EdgeWorkerHeartbeatResponse,
    EdgeWorkerRegisterRequest,
    EdgeWorkerResponse,
    EntityVersionResponse,
    HardwareMetricsSchema,
    ResolveDisputeRequest,
    ResolveDisputeResponse,
    SyncBatchSubmitRequest,
    SyncBatchSubmitResponse,
    SyncQueueItemSchema,
)
from backend.edge import (
    ConflictResolutionPolicy,
    EdgeWorker,
    HardwareMetrics,
    HeartbeatPayload,
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
    sync_coordinator,
)
from datetime import datetime


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


