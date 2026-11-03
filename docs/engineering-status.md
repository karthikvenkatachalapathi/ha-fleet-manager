# Engineering status — preserved audit continuation

## Authority and boundaries

- Contract: `docs/fleet-manager-engineering-directive.md`, commit `2f1e48fab6ff232f0028110d7b4f75d56648ce07`; read in full before resumption.
- Execution owner: Hermione. Luna supports routine analysis. Dumbledore reviews independently, without implementing or running tests.
- Production remains unchanged. No HA mutations, production database changes, service restarts, protected merges, or release publication are authorized.
- Candidate verdict: **HOLD**. No production-readiness or completed browser-acceptance claim.

## Continuity and preserved work

The existing audit was resumed, not restarted. Original baseline, corrected candidate, frozen safety-v2 milestone, 60-route inventory, regression artifacts, and independent reviews remain preserved in the original private audit workspace.

The live checkout is intentionally not stashed, switched, rebased, merged, or pulled in place: it contains deployed, uncommitted work. Latest remote changes were fetched, and a separate development worktree was based on the directive commit. Local commit `d352059` was merged into that development branch to preserve existing refresh/asset regressions without modifying the live checkout. Tracked-diff and source/status hash preservation records are private evidence. The deployed source is a dirty tree, not accurately described by a single commit SHA.

Frozen corrected candidate hashes match the prior passing regression manifest exactly. Prior executed evidence: 180 baseline backend tests, 81 frontend tests; 13 targeted safety cases, initially 12 failing; corrected candidate 193 backend tests passing with eight deprecation warnings. These are historical isolated results, not tests of this tracking-only commit or user-browser acceptance.

## GitHub tracking

- Parent: #1.
- Issues #2–#20 record individual engineering workstreams, severity, acceptance criteria, evidence, and regression requirements.
- Existing repository state at resumption: main only, no PRs/issues/releases/projects, tag `v0.1.0`, and only dependency-graph automation.
- GitHub Issues creation works and each issue body was read back, including its disclosure footer.
- Project creation is blocked by the current token's permission denial. Desired states: Triage → Ready → In Progress → In Review → Validation → Done; Blocked separately.
- Label creation is also denied; classification is recorded in issue bodies instead. Do not expand credentials or modify repository protections to bypass this.
- Private repository advisory creation is denied. Sensitive unpatched details stay private; they are not reproduced in public issues, PRs, or this document. Owner approval/access correction is needed for restricted advisory tracking.
- Main protection details cannot be read with the current token. No merge is attempted regardless.

## Active work and ordering

1. #11 In Progress: inert imports, explicit bootstrap/schema compatibility, and conservative legacy preservation are prerequisites for a reliable safety contract.
2. #2–#5: verify preserved durable dispatch, ambiguity handling, backup lock/fallback, and shared home holds. Remaining behavior is not cleared by their narrow passing tests.
3. #6: revision-fenced operation transitions and stale-worker protection.
4. #7–#10: explicit recovery, source identity/history, truthful activation evidence, and dispatch-adjacent policy.
5. #12–#13: cross-process scheduler ownership and bounded, failure-isolated I/O.
6. Restricted security remediation proceeds locally while advisory tracking access is blocked.
7. #14–#18: freshness, honest UI states, exact bounded history, secondary effects, rendered desktop/mobile accessibility.
8. #19–#20: isolated CI, migration/rollback, security validation, final independent review, and release-candidate readiness packet.

No issue is Done. A review HOLD creates further work. An open PR or permission blocker does not suspend independent engineering.

## Resumed implementation checkpoint — November 3, 2026

- Hermione continued in the isolated implementation worktree. Private implementation is preserved in local commits `230b0d3`, `6daebce`, and `2a78e96`; it has not been deployed or published. Sensitive changes and detailed evidence remain private while restricted tracking permissions are unresolved.
- Explicit offline schema lifecycle, transactional schema maintenance, literal-aware compatibility checks, revision-fenced dispatch/observations, and stale-write conflict handling have been implemented and regression-tested. This is not completion of #6 or #11: inherited recovery/history and lifecycle acceptance remain open.
- Dumbledore's second independent read-only review passed its three narrow revision/observation/schema re-review items but retained overall HOLD and identified a diagnostic transaction-boundary defect. Hermione reproduced that defect with a real second SQLite writer, corrected it, and verified the regression. The third frozen-source review passed the scoped diagnostic correction and narrow UI corrections, identified no new significant regression within that scope, and retained overall HOLD.
- Exact final frozen candidate: `schema-fence-resumed-v3`, 98 files. The same source hashes match the final backend, frontend, and browser evidence manifests. Subsequent unchanged-source reruns also record execution-time hashes for private verification helpers and baseline inputs; these 23 supporting files are separately frozen. Earlier reviews are not represented as review of later helper changes.
- Executed synthetic verification: 234 repository Python tests plus one private preserved-baseline upgrade rehearsal passed (235 total, six deprecation warnings); 84 Node frontend tests passed; four real Chromium intercepted-fixture journeys passed their explicit assertions. Tests ran without host-network access; database tests used private scratch tmpfs and synthetic credentials/HOME/database. These are bounded development results, not production, SSO, real HA, user-browser acceptance, or a full security clearance.
- A separate next-workstream history-preservation regression is intentionally red. Its failure is privately preserved and is not hidden by the 235-test passing subset. No release-ready or all-green claim is made.
- Active production user service was read-only checked and remained running with its original PID. No production checkout/database/service or HA action was changed.
- Existing GitHub permission failures do not block local engineering. Project, PR, restricted advisory, and issue-update limitations must not be bypassed by broader credentials.

## History/source milestone — November 3, 2026

- Hermione completed another isolated implementation checkpoint, preserved privately in local commit `8493fb2`. No implementation/security patch, raw logs, databases or credentials are published with this tracking update.
- History-preserving retirement, source-bound admission/observations, conservative one-shot submission, lossless approval/recovery evidence and usable human cancellation preconditions were implemented and regression-tested. Earlier failures and each intermediate candidate remain preserved; the original destructive-history acceptance probe now passes.
- Four independent static review rounds drove further reproductions and corrections. Dumbledore's final scoped review passed the remaining acknowledgement correction and carried forward the unchanged narrow positive findings. Overall release remains **HOLD**. Dumbledore did not implement, debug, import the application or execute tests.
- Exact reviewed candidate: `history-source-v4`, 108 source/test/document files; 23 execution-support inputs separately frozen. Backend, frontend and browser execution-time hashes agree with that candidate. Private cumulative implementation patch and all prior candidates/support manifests are retained.
- Executed final synthetic verification: **469 Python tests passed** (467 repository tests plus the preserved-baseline upgrade rehearsal and original history-hold probe), seven deprecation warnings; **84 Node tests passed**; **four limited real Chromium intercepted-fixture journeys passed** their explicit assertions. These are development results, not real HA/SSO/backend-integrated browser acceptance, production readiness or complete security/accessibility coverage.
- Independent next-workstream acceptance is still red: **one multiprocess scheduler-ownership probe and four monitoring/secondary-effect transaction-boundary probes fail**. They are separately preserved and are **not included in the 469-pass subset**. Do not describe the entire engineering acceptance as all-green.
- Dumbledore reviewed the scheduler ownership proposal and permitted isolated implementation **with conditions**, not implementation acceptance. Next: enforce correct execution-authority/worker-lifetime ownership, fix reproduced writer-held I/O boundaries, cover process crash/cancellation and real CLI/HTTP contention, then freeze/review the changed source. Compound-action stage identity, quiescence recovery, security, broader UI/accessibility/performance and release work remain open.
- GitHub inventory read at **09:28 CT**: **19 remediation issues open, 0 closed**, excluding umbrella #1; no PR exists. Newly closed: 0; total closed: 0/19 (**0% by issue count**, not zero effort or production readiness). No issue is closed by these scoped passes.
- Production checkout/database/services and managed HA actions remain unchanged. Existing permission limits apply only to affected tracking features; no credential expansion or protected operation is attempted. No new user input is needed for the next isolated engineering work. Persisted checkpoint text does not imply an unattended worker remains running after its owning process exits.

## Ownership continuation — paused at execution limit

- Hermione continued isolated ownership work and preserved an immutable prototype. Its original competing-process regression and a 499-case backend subset, 84 Node tests and four limited Chromium fixture journeys passed. A separate new database-lock-preservation regression then invalidated that prototype. The passing subset is historical evidence, not acceptance; the unsafe prototype is rejected and retained privately.
- Dumbledore completed a second independent static design review: **PROCEED WITH CONDITIONS**, design only; overall **HOLD**. No implementation, debugging, test execution or source changes were delegated to the reviewer. The proposed replacement and required storage, authority, lifecycle and cleanup conditions remain distinct from implementation acceptance.
- A corrected ownership mechanism is staged only in the private, uncommitted worktree. Latest focused execution: **47 passed, one failed**. The remaining failure concerned synthetic fixture permissions; the fixture was corrected, but that last edit has **not been rerun**. Current source is not claimed fully passing, frozen, source-reviewed or ready to port/deploy. The earlier 499/84/four results do not validate this newer source.
- Four separate real monitoring/secondary-effect writer-boundary failures remain unresolved. Next work: rerun the focused suite, complete authority/topology/invalid-entry and failure-cleanup evidence, resolve transaction boundaries without stale decisions, rerun broader suites, freeze exact changed source/support and obtain independent source review.
- The platform explicitly exhausted this execution window. Work is **paused, not complete**. Exact dirty-source state, preserved failures, review conditions, next commands and the unverified last edit are recorded in the existing private continuation checkpoint. No engineering continuation is claimed after the owning process exits.
- Newly closed: 0. Last verified inventory: **19 remediation issues open, 0 closed**, excluding #1; **0/19 = 0% by issue count**, not effort or readiness. No fresh inventory or newer remote checkpoint is claimed by this local-only update. No new user input is needed for the next isolated work. Production, HA, services, credentials, profiles, skills, schedules and vault remain unchanged.

## Supported continuation — sidecar source under independent review

1. Completed / closed: Hermione resumed the existing authorized work and preserved all prior files and rejected prototypes. The exact checkpoint rerun passed 48 tests. Real isolated mount/namespace and SQLite lock-preservation evidence was added. Independent static source review retained HOLD and identified additional authority/cleanup/outcome requirements; these were reproduced and corrected locally, with conservative cleanup quarantine rather than speculative takeover. The newer frozen private candidate has 617 passing backend tests, 84 Node tests and four limited intercepted-fixture Chromium journeys. Scoped re-review is running; source acceptance is not claimed. Newly closed: 0; total closed: 0/19.
2. Open / next: GitHub read at 11:38 CT verified 19 open remediation issues and 0 closed, excluding umbrella #1; no PR. Four separate real monitoring writer-boundary probes were freshly rerun and all four still fail. Independent monitoring-design review also retained HOLD; its revised proposal is under static re-review. Next: remediate remaining source/design findings, then implement fenced monitoring observation/review/dispatch/notification publication without retaining writers over I/O. No all-green or release-readiness claim.
3. Blockers / input: No new user input is required for isolated engineering. Existing tracking-feature permission limits remain scoped to those features. Production, services, HA, credentials, profiles, skills, schedules and vault are unchanged. All independent reviewers were restricted to frozen static evidence; none implemented, debugged or ran tests.
4. ETA / percentage: 6–14 engineering days remaining, uncertain; reassess after transaction/recovery/security evidence. 0/19 = 0% by issue count only, not zero effort or readiness. Overall release HOLD. A progress update or reviewer HOLD does not end authorized execution; this checkpoint does not promise execution after its owning process exits.

## Estimate

Remaining estimate: **6–14 engineering days**, not a delivery guarantee. Re-estimate after schema/recovery and security findings are experimentally bounded. Owner permission delays and production/user acceptance are not represented as engineering time.

## Next reproducible checkpoint

Continue from the frozen private implementation and exact evidence, not the older tracking-only source. Prioritize the reproduced history-preservation/source-identity lifecycle failure and explicit operator recovery without losing unresolved holds or audit lineage. Retain intentionally failing acceptance probes alongside passing scoped suites. Continue scheduler/secondary-effect/security and broader rendered acceptance work afterward. Port only reviewed/sanitized source and synthetic tests; never private logs, databases, credentials, infrastructure details, or the full audit folder. No issue closure, protected merge, deployment, or release follows from a narrow pass.

*Generated by AI*
