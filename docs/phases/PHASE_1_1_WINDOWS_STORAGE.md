# Phase 1 follow-up: concurrent local object storage

The Windows acceptance run reported 57 tests, one error and no skips. The error
was `object path escaped configured root` in the concurrent ingestion test. It
came from comparing a resolved destination filename to a cached root path.

The failure is in CitePrism's local storage implementation. The traceback does
not include both path values, so it does not establish exactly which Windows
alias or resolution race caused that comparison to fail. Python documents that
non-strict resolution can combine resolved existing components with unresolved
missing components: [os.path.realpath](https://docs.python.org/3.12/library/os.path.html#os.path.realpath).

## Change

- Create the configured root before resolving it strictly and retaining its
  filesystem identity. A configured root alias is resolved once at initialization.
- Derive object paths only from the existing validated relative key grammar.
  Never resolve the digest filename while another writer may be publishing it.
- Walk directories one component at a time. Inspect entries with `lstat`; reject
  symlinks and Windows reparse points, including junctions, inside the store.
  Recheck directories when a competing writer creates them first.
- Reject a removed/replaced root, non-directory path components, and non-regular
  object entries. Keep create-only hard-link publication, digest verification,
  immutable filenames and idempotent duplicate writes.

Managed directories must be ordinary directories below the configured root.
Cloud placeholders/reparse points inside the store are intentionally unsupported.
This remains an application-managed filesystem boundary, not protection against
a hostile administrator swapping directories between individual OS operations.
The local filesystem must support hard links; no overwrite fallback is added.

## Verification

Six new tests cover alternate filename resolution, concurrent independent store
instances, existing and raced-in redirected directories, redirected object entries,
and replaced roots/non-directory components.

The deterministic resolver test produces the reported exception with the original
implementation and passes after the change. This emulates the unstable resolver
boundary; it is not a claim of reproducing the native Windows API behavior here.

The local suite reports 63 discovered tests: 55 pass, 8 PostgreSQL tests skip.
Both shared-instance ingestion and independent-instance publication were also
repeated 25 times each (50 test executions), all passing on Linux/Python 3.12.
Compilation and whitespace checks pass. The native Windows rerun is still needed:

```powershell
& .\scripts\verify_phase1.ps1 -Python .\.venv-phase1\Scripts\python.exe -WithPostgres
```

Expected acceptance: 63 tests, OK, no skips. Existing Docker data and environment
are retained. No database migration change or dependency installation is needed.

## Local development and later handoff

Apply the follow-up patch to the already-applied Phase 1 working files using
`git apply --check`, then `git apply`, without `--index`. Do not require a clean
working tree: Phase 1 is intentionally still uncommitted after the failed check.
Do not reset or reapply the original Phase 1 patch. Review and commit locally after
verification succeeds. Local commits publish nothing. Keep the existing remote
unchanged and do not push to it; a separate repository for Janvi's pull-based
handoff will be created later.
