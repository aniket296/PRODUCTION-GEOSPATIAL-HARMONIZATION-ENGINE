# SIH26013: Multi-Source Geospatial Data Harmonization Engine for Urban Land Records

**Sponsoring Organization:** Ministry of Rural Development  
**System Architecture:** Production Multi-Source Geospatial Data Harmonization Engine

## 1. Overview

The **Geospatial Harmonization Engine** is an enterprise-grade, offline-first geospatial processing platform engineered for urban land records. It harmonizes heterogeneous cadastral datasets (Shapefile, GeoJSON, KML/KMZ, DXF) across diverse legacy coordinate reference systems (Everest 1830, Kalianpur 1975, WGS 84, UTM 43N/44N), identifies topological anomalies (overlaps, slivers, gaps, dangling nodes), deterministically heals auto-resolvable defects, protects critical land disputes, and streams high-performance FlatGeobuf/GeoPackage exports.

## 2. Core Architectural Pillars

- **Offline-First Runtime:** Operates completely without Internet access. No external cloud APIs, external geocoders, or external tile servers. Local basemaps, PostGIS, Redis, Celery, and MinIO storage.
- **Deterministic Topology Engine:** PostGIS-backed spatial validation adhering to strict mathematical thresholds:
  - Overlap $\le 2\%$: `AUTO_RESOLVABLE` (centerline split & assignment).
  - Overlap $> 2\%$: `CRITICAL_DISPUTE` (geometry preserved & dispute locked).
  - Slivers: Metric area $< 10\text{ m}^2$ AND Polsby-Popper compactness $< 0.05$ (longest shared border union).
  - Boundary Gaps: $0.5\text{ m}$ detection threshold, $0.25\text{ m}$ snapping tolerance via `ST_Snap`.
  - Floating Point Drift: `ST_SnapToGrid` at $0.001\text{ m}$ precision.
- **Authoritative Geodetic Transformation:** PyProj-based Bursa-Wolf parametric coordinate transformation pipelines between legacy Indian datums and modern UTM projections.
- **Asynchronous Processing:** Celery worker queue with Redis broker for long-running batch ingestion and geometry repair.
- **High-Performance Streaming:** FlatGeobuf HTTP byte-range feature streaming (`206 Partial Content`) for instant client rendering.
- **Dual Differential GIS Viewer:** MapLibre GL JS synchronized dual-viewports for before/after visual inspection with conflict overlays.

## 3. Technology Stack

- **Backend:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x (async), GeoAlchemy2, asyncpg, Alembic.
- **Geospatial Libraries:** GDAL/OGR, GEOS, Shapely 2.x, PyProj 3.6+, GeoPandas 1.x.
- **Database:** PostgreSQL 16 + PostGIS 3.4 (`postgis`, `postgis_topology`, `pgcrypto`).
- **Distributed Queue & Cache:** Redis 7 Alpine, Celery 5.4+.
- **Object Storage:** MinIO (S3-compatible, buckets: `cadastral-raw`, `cadastral-harmonized`).
- **Frontend:** React, TypeScript, Vite, Tailwind CSS, Shadcn UI, Zustand, Axios, MapLibre GL JS.

## 4. Directory Structure

```
├── .dockerignore
├── .env.example
├── .gitignore
├── README.md
├── backend/              # FastAPI application, GeoAlchemy2 models, Celery tasks
├── docker/               # Dockerfiles, compose definitions, entrypoints
├── docs/                 # System architecture, geodetic parameters, API specs
├── fixtures/             # Deterministic synthetic test fixtures
├── frontend/             # React + Vite + MapLibre GL JS UI
└── scripts/              # Operational and maintenance automation
```
