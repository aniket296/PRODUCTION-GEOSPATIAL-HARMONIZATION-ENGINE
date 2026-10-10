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
