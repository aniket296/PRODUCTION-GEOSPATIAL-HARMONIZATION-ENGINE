# ==============================================================================
# SIH26013: Multi-Source Geospatial Data Harmonization Engine
# Multi-Stage Backend Dockerfile: Python 3.12, GDAL, GEOS, PROJ (Step 8/150)
# ==============================================================================

# ------------------------------------------------------------------------------
# Stage 1: Builder Stage
# ------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install build toolchains and geospatial compilation dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    pkg-config \
    gcc \
    g++ \
    git \
    curl \
    libpq-dev \
    libgdal-dev \
    libgeos-dev \
    libproj-dev \
    proj-bin \
    proj-data \
    gdal-bin \
    && rm -rf /var/lib/apt/lists/*

# Establish Python virtual environment for isolated dependency resolution
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Upgrade core packaging tools
RUN pip install --upgrade pip setuptools wheel

# ------------------------------------------------------------------------------
# Stage 2: Runtime Stage
# ------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install shared runtime geospatial libraries without compilation toolchains
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libpq5 \
    gdal-bin \
    libgdal32 \
    libgeos-c1v5 \
    libproj25 \
    proj-bin \
    proj-data \
    && rm -rf /var/lib/apt/lists/*

# Geospatial environment paths for PROJ and GDAL
ENV PROJ_LIB=/usr/share/proj \
    GDAL_DATA=/usr/share/gdal \
    CPLUS_INCLUDE_PATH=/usr/include/gdal \
    C_INCLUDE_PATH=/usr/include/gdal \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Copy virtual environment from builder stage
COPY --from=builder /opt/venv /opt/venv

# Create dedicated non-root application user and group
RUN groupadd -g 1000 cadastre_user && \
    useradd -u 1000 -g cadastre_user -m -s /bin/bash cadastre_user && \
    mkdir -p /app /app/backend && \
    chown -R cadastre_user:cadastre_user /opt/venv /app

WORKDIR /app

# Switch to non-root cadastre_user
USER cadastre_user

# Smoke test geospatial CLI binaries and Python virtual environment in container
RUN gdalinfo --version && geos-config --version && proj && python3 --version
