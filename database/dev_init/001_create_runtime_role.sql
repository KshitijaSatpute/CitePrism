-- Local development only. Never use these published credentials in deployment.
CREATE ROLE citeprism_app LOGIN PASSWORD 'citeprism-app-dev-only'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
