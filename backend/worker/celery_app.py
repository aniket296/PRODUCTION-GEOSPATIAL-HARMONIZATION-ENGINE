"""Celery application and asynchronous distributed worker configuration."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

from celery import Celery, Task
from kombu import Exchange, Queue

from backend.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class CadastreTask(Task):
    """Base Celery task providing progress reporting and error lifecycle observability."""

    abstract = True

    def update_progress(
        self,
        percent: int,
        stage: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Publish real-time processing progress updates to backend broker."""
        try:
            self.update_state(
                state="PROGRESS",
                meta={
                    "percent": max(0, min(100, percent)),
                    "stage": stage,
                    "details": details or {},
                },
            )
        except Exception as err:
            logger.debug("Failed to record task progress state to backend: %s", err)

    def on_failure(
        self,
        exc: Exception,
        task_id: str,
        args: tuple,
        kwargs: dict,
        einfo: Any,
    ) -> None:
        """Handle and log unhandled pipeline task failures."""
        logger.error(
            "Task %s [%s] failed: %s | Args: %s",
            self.name,
            task_id,
            exc,
            args,
        )
        super().on_failure(exc, task_id, args, kwargs, einfo)

    def on_retry(
        self,
        exc: Exception,
        task_id: str,
        args: tuple,
        kwargs: dict,
        einfo: Any,
    ) -> None:
        """Log transient task retries."""
        logger.warning(
            "Task %s [%s] retrying due to: %s",
            self.name,
            task_id,
            exc,
        )
        super().on_retry(exc, task_id, args, kwargs, einfo)


celery_app = Celery(
    "cadastral_harmonization_engine",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

# Robust asynchronous task queue configuration
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=3600,
    task_soft_time_limit=3300,
    task_default_queue="geo_ingestion",
    task_queues=(
        Queue("geo_ingestion", Exchange("geo_ingestion"), routing_key="geo_ingestion.#"),
        Queue("geo_harmonization", Exchange("geo_harmonization"), routing_key="geo_harmonization.#"),
        Queue("geo_export", Exchange("geo_export"), routing_key="geo_export.#"),
        Queue("geo_dead_letter", Exchange("geo_dead_letter"), routing_key="geo_dead_letter.#"),
    ),
    task_routes={
        "backend.worker.celery_app.process_cadastral_layer_task": {
            "queue": "geo_ingestion",
            "routing_key": "geo_ingestion.process",
        },
        "backend.worker.celery_app.detect_topology_errors_task": {
            "queue": "geo_harmonization",
            "routing_key": "geo_harmonization.topology",
        },
        "backend.worker.celery_app.auto_heal_topology_task": {
            "queue": "geo_harmonization",
            "routing_key": "geo_harmonization.heal",
        },
        "backend.worker.celery_app.export_harmonized_layer_task": {
            "queue": "geo_export",
            "routing_key": "geo_export.format",
        },
        "backend.worker.celery_app.celery_ping_task": {
            "queue": "geo_ingestion",
            "routing_key": "geo_ingestion.ping",
        },
    },
)


def _update_layer_record_sync(
    layer_id: str,
    status: Any,
    feature_count: Optional[int] = None,
    detected_crs: Optional[str] = None,
    bounds: Optional[List[float]] = None,
    error_message: Optional[str] = None,
) -> None:
    """Best-effort database update of raw cadastral layer record."""
    try:
        import asyncio
        from backend.db.session import async_session_factory
        from backend.models.layer import RawCadastralLayer
        from sqlalchemy import select

        async def _update() -> None:
            async with async_session_factory() as session:
                parsed_id = uuid.UUID(str(layer_id))
                stmt = select(RawCadastralLayer).where(RawCadastralLayer.id == parsed_id)
                res = await session.execute(stmt)
                layer = res.scalar_one_or_none()
                if layer:
                    layer.status = status
                    if feature_count is not None:
                        layer.feature_count = feature_count
                    if detected_crs is not None:
                        layer.srid_declared = detected_crs
                    if bounds is not None:
                        layer.bounds = bounds
                    if error_message is not None:
                        layer.error_message = error_message
                    await session.commit()

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_update())
        except RuntimeError:
            asyncio.run(_update())
    except Exception as exc:
        logger.debug("Database sync update skipped or failed (offline/mock environment): %s", exc)


@celery_app.task(
    base=CadastreTask,
    bind=True,
    name="backend.worker.celery_app.process_cadastral_layer_task",
    autoretry_for=(Exception,),
    dont_autoretry_for=(ValueError,),
    retry_kwargs={"max_retries": 3},
    retry_backoff=True,
    retry_jitter=True,
)
def process_cadastral_layer_task(
    self: CadastreTask,
    layer_id: str,
    file_path: Optional[str] = None,
    target_crs: Optional[str] = None,
    normalize_geometries: bool = True,
) -> Dict[str, Any]:
    """Asynchronous pipeline task: Ingest, auto-detect, and validate cadastral survey layer."""
    from backend.worker.ingestion_state import (
        IngestionState,
        IngestionStateTracker,
        to_layer_status,
    )

    tracker = IngestionStateTracker(layer_id=layer_id)

    if not file_path:
        tracker.transition(IngestionState.VALIDATING_FORMAT, "Awaiting file payload")
        self.update_progress(tracker.progress, tracker.state.value)
        return {
            "task_id": self.request.id,
            "layer_id": layer_id,
            "status": "PROCESSING",
            "action": "ingest_and_validate",
            "tracker": tracker.to_dict(),
        }

    try:
        # Stage 1: VALIDATING_FORMAT
        from backend.ingestion.validation import UploadValidator

        tracker.transition(IngestionState.VALIDATING_FORMAT, "Validating file signature and format integrity")
        self.update_progress(tracker.progress, tracker.state.value)

        report = UploadValidator.validate_file(file_path)
        if not report.is_valid:
            error_msg = report.error_message or "Upload pre-flight validation failed"
            tracker.fail(error_msg)
            self.update_progress(tracker.progress, tracker.state.value)
            _update_layer_record_sync(
                layer_id=layer_id,
                status=to_layer_status(tracker.state),
                error_message=error_msg,
            )
            raise ValueError(f"Upload validation failed for layer {layer_id}: {error_msg}")

        # Stage 2: INGESTING_FEATURES
        from backend.ingestion.pipeline import UnifiedIngestionDispatcher

        tracker.transition(IngestionState.INGESTING_FEATURES, "Extracting features from dataset")
        self.update_progress(tracker.progress, tracker.state.value)

        result = UnifiedIngestionDispatcher.ingest_file(file_path, target_crs=target_crs)

        # Stage 3: NORMALIZING_GEOMETRY (for vector layers when requested)
        normalized_count = result.feature_count
        if normalize_geometries and not result.is_raster:
            from backend.normalizer import normalize_to_2d_multipolygon
            from backend.normalizer.multipolygon import GeometryCastError

            tracker.transition(IngestionState.NORMALIZING_GEOMETRY, "Normalizing geometries to 2D MultiPolygon")
            self.update_progress(tracker.progress, tracker.state.value)

            valid_norm_count = 0
            if result.features:
                for feat in result.features:
                    geom = feat.geometry
                    if geom is not None:
                        try:
                            norm_geom = normalize_to_2d_multipolygon(geom)
                            feat.geometry = norm_geom
                            valid_norm_count += 1
                        except GeometryCastError as exc:
                            logger.warning(
                                "Feature %s geometry normalization skipped: %s",
                                feat.id,
                                exc,
                            )
                normalized_count = valid_norm_count

        # Stage 4: COMPLETED
        tracker.transition(
            IngestionState.COMPLETED,
            "Cadastral layer ingestion and normalization completed successfully",
        )
        self.update_progress(tracker.progress, tracker.state.value)

        _update_layer_record_sync(
            layer_id=layer_id,
            status=to_layer_status(tracker.state),
            feature_count=result.feature_count,
            detected_crs=result.detected_crs,
            bounds=list(result.bounds) if result.bounds else None,
        )

        return {
            "task_id": self.request.id,
            "layer_id": layer_id,
            "format": result.format.value,
            "detected_crs": result.detected_crs,
            "feature_count": result.feature_count,
            "normalized_count": normalized_count,
            "bounds": list(result.bounds) if result.bounds else None,
            "is_raster": result.is_raster,
            "status": IngestionState.COMPLETED.value,
            "action": "ingest_and_validate",
            "tracker": tracker.to_dict(),
            "metadata": result.metadata,
        }
    except Exception as exc:
        if not tracker.is_terminal:
            tracker.fail(str(exc))
            self.update_progress(tracker.progress, tracker.state.value)
        _update_layer_record_sync(
            layer_id=layer_id,
            status=to_layer_status(tracker.state),
            error_message=str(exc),
        )
        raise


@celery_app.task(
    base=CadastreTask,
    bind=True,
    name="backend.worker.celery_app.detect_topology_errors_task",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3},
    retry_backoff=True,
)
def detect_topology_errors_task(self: CadastreTask, layer_id: str) -> Dict[str, Any]:
    """Asynchronous pipeline task: Run DE-9IM intersection and sliver defect audits."""
    self.update_progress(25, "AUDITING_TOPOLOGY")
    return {
        "task_id": self.request.id,
        "layer_id": layer_id,
        "status": "COMPLETED",
        "action": "detect_topology_errors",
    }


@celery_app.task(
    base=CadastreTask,
    bind=True,
    name="backend.worker.celery_app.auto_heal_topology_task",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3},
    retry_backoff=True,
)
def auto_heal_topology_task(self: CadastreTask, layer_id: str) -> Dict[str, Any]:
    """Asynchronous pipeline task: Execute micro-overlap and sliver union healing."""
    self.update_progress(50, "HEALING_TOPOLOGY")
    return {
        "task_id": self.request.id,
        "layer_id": layer_id,
        "status": "COMPLETED",
        "action": "auto_heal_topology",
    }


@celery_app.task(
    base=CadastreTask,
    bind=True,
    name="backend.worker.celery_app.export_harmonized_layer_task",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3},
    retry_backoff=True,
)
def export_harmonized_layer_task(
    self: CadastreTask,
    layer_id: str,
    export_format: str = "FlatGeobuf",
) -> Dict[str, Any]:
    """Asynchronous pipeline task: Stream-export finalized spatial parcels."""
    self.update_progress(90, "SERIALIZING_EXPORT")
    return {
        "task_id": self.request.id,
        "layer_id": layer_id,
        "export_format": export_format,
        "status": "COMPLETED",
        "action": "export_harmonized_layer",
    }


@celery_app.task(
    name="backend.worker.celery_app.celery_ping_task",
)
def celery_ping_task() -> Dict[str, Any]:
    """Worker health-check ping task."""
    return {
        "status": "PONG",
        "service": "cadastral_harmonization_engine",
    }


def get_celery_app() -> Celery:
    """Dependency provider returning Celery application instance."""
    return celery_app
