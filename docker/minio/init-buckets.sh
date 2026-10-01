#!/bin/sh
# ==============================================================================
# SIH26013: Multi-Source Geospatial Data Harmonization Engine
# MinIO Bucket Initialization Script (Step 7/150)
# ==============================================================================

set -e

ENDPOINT="${MINIO_ENDPOINT:-minio:9000}"
ACCESS_KEY="${MINIO_ROOT_USER:-minio_admin}"
SECRET_KEY="${MINIO_ROOT_PASSWORD:-minio_secure_password}"
BUCKET_RAW="${MINIO_BUCKET_RAW:-cadastral-raw}"
BUCKET_HARMONIZED="${MINIO_BUCKET_HARMONIZED:-cadastral-harmonized}"

echo "Configuring MinIO client alias for http://${ENDPOINT}..."
until /usr/bin/mc alias set myminio "http://${ENDPOINT}" "${ACCESS_KEY}" "${SECRET_KEY}"; do
    echo "Waiting for MinIO service to become ready..."
    sleep 2
done

echo "Ensuring required cadastral buckets exist..."
/usr/bin/mc mb --ignore-existing "myminio/${BUCKET_RAW}"
/usr/bin/mc mb --ignore-existing "myminio/${BUCKET_HARMONIZED}"

echo "MinIO buckets successfully verified:"
/usr/bin/mc ls myminio

echo "MinIO initialization complete."
exit 0
