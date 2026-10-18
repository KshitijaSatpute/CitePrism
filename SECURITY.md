# Security status

CitePrism Phase 3 is an alpha research-review foundation. Do not represent it as
government-authorized, certified, production-hardened, or proven against competitors.

Report suspected issues privately to the university/project maintainers through
your established collaboration channel. Do not include live secrets or sensitive
manuscripts in public issue reports. No security contact address or response SLA
has been assigned in this repository.

See `docs/architecture/THREAT_MODEL.md` for implemented controls and gaps. Before deployment:
deploy `trusted_proxy` mode behind an identity-aware gateway that strips client
identity headers; validate the tenant isolation boundary; separate migration
credentials; rotate public dev passwords; configure
TLS, secret management, IAM, backups/restore, retention, audit anchoring and incident
response; scan dependencies/build artifacts; and test accessibility, performance,
model quality and the target customer's authorization requirements. Local mode is
for a single developer workstation and is not an authentication mechanism.
