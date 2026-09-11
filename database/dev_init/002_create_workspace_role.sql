-- Development-only unprivileged runtime role for the Phase 4 workspace schema.
-- Production creates this role through its own account-management process and
-- never stores the password in the repository.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'citeprism_workspace_app') THEN
        CREATE ROLE citeprism_workspace_app LOGIN NOINHERIT
            PASSWORD 'citeprism-workspace-dev-only';
    END IF;
END
$$;
REVOKE ALL ON DATABASE citeprism_workspace FROM PUBLIC;
GRANT CONNECT ON DATABASE citeprism_workspace TO citeprism_workspace_app;
