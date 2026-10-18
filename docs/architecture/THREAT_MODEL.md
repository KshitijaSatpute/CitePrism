# Trust foundation threat model

Scope: provenance of imported citation-review artifacts. Not a guarantee of
scientific truth, research misconduct, agency authorization, or regulatory compliance.

| Threat / failure | Implemented control | Residual boundary |
|---|---|---|
| Same filename replaces prior bytes | SHA-256 addressed objects and version records | Local administrator can alter files; no WORM filesystem |
| Partial import leaves an apparently complete ledger | One transaction for imported entities and audit events | Object store / SQLite are outside that transaction; orphan cleanup pending |
| Retry duplicates evidence | Deterministic import IDs and exact-content idempotency | Changed reference extraction currently conflicts |
| Reference points at another document | Tenant+version foreign keys and subject validation | Depends on trusted schema administrator |
| Missing tenant predicate leaks rows | Forced RLS and transaction tenant setting | Shared runtime credential can select another tenant; no user auth boundary yet |
| Runtime edits or deletes ledger rows | INSERT/SELECT grants and mutation triggers | Owner/superuser can change grants, schema or triggers |
| Record altered without matching audit update | Entity digest binding plus hash-chain verification | Full-chain rewrite/truncation with matching entity rewrite needs external signed anchors to detect |
| Runtime inserts directly without audit | Snapshot verifier detects unaudited entities | SQL INSERT grants cannot force use of repository; detection, not prevention |
| Provider output is hallucinated or malicious | Imported content stored as untrusted automated signals | Independent source verification and prompt-injection defences pending |
| User spoofs reviewer identity | No public reviewer API exposed by this phase | Service accepts caller identity; authentication/authorization must precede exposure |
| Malicious or oversized PDF | Existing signature and byte-size limits | No malware scanning, sandboxed parsing, or page-count/resource budget |
| S3 overwrite / missing checksum | Conditional create; version IDs; checksum or pinned GET rehash | IAM, Object Lock, approved key policy and restore drills are deployment work |

The canonical format is Python/Pydantic sorted compact JSON with UTF-8 and no
NaN/Infinity; it is not claimed to implement RFC 8785. Preserve version-1 encoding
or introduce a versioned migration when changing cross-language canonicalization.

Never place the admin DSN in the application environment. Keep public development
credentials on localhost only. Do not upload secrets, personal data, or restricted
research datasets to external model services without the data owner's approved
processing path. University collaboration does not itself settle deployment rules.
