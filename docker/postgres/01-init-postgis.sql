-- ==============================================================================
-- SIH26013: Multi-Source Geospatial Data Harmonization Engine
-- PostGIS & Spatial Extensions Initialization (Step 4/150)
-- ==============================================================================

\connect cadastre_db

-- Enable core cryptographic and UUID primitives
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Enable PostGIS spatial engine
CREATE EXTENSION IF NOT EXISTS "postgis";

-- Enable PostGIS Topology schema and functions
CREATE EXTENSION IF NOT EXISTS "postgis_topology";

-- Set search path to include public and topology schemas
ALTER DATABASE cadastre_db SET search_path TO public, topology;

-- Grant permissions to cadastre_user on topology schema
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cadastre_user') THEN
        GRANT USAGE, CREATE ON SCHEMA topology TO cadastre_user;
        GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA topology TO cadastre_user;
        GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA topology TO cadastre_user;
        ALTER DEFAULT PRIVILEGES IN SCHEMA topology GRANT ALL ON TABLES TO cadastre_user;
        ALTER DEFAULT PRIVILEGES IN SCHEMA topology GRANT ALL ON SEQUENCES TO cadastre_user;
    END IF;
END $$;
