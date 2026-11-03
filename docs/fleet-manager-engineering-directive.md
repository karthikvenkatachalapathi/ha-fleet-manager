Fleet Manager — Continuous Engineering, Comprehensive Audit, Remediation, GitHub Governance, and Release Readiness

/bg

1. Mission and Standing Directive

Continue the existing Fleet Manager engineering audit and remediation effort. This is a continuation of previously completed work, not a new project or another initial assessment.

Move from reactive fixes toward comprehensive engineering focused on architectural correctness, reliability, security, CAP principles, usability, performance, maintainability, and eventual public release.

Your mission is to independently drive Fleet Manager toward production readiness, with evidence-backed validation and a sustainable GitHub development process.

MANDATORY: CONTINUOUS EXECUTION. Continue autonomously while actionable work remains and the execution environment permits. Do not stop after completing an issue, opening a PR, receiving a review, reporting progress, or finishing a milestone. A meaningful progress update must not terminate execution.

2. Canonical Repository and Durable Engineering State

GitHub: https://github.com/karthikvenkatachalapathi/ha-fleet-manager

Existing audit workspace: /home/hermes/projects/fleet-manager-system-audit-20261103

GitHub is the canonical engineering source of truth. The audit workspace is for isolated execution and evidence, not the sole record of progress.

Before making changes, inspect existing branches, issues, PRs, releases, tags, CI, and documentation. Preserve uncommitted changes; identify the deployed production revision; avoid duplicate work; verify permissions; and keep secrets and sensitive infrastructure details out of the public repository.

Issues and project

Create or update one issue per actionable defect, architectural concern, security finding, or enhancement. Record severity, priority, component, reproduction, evidence, root cause where known, expected behavior, acceptance criteria, regression requirements, dependencies, and release target. Use private GitHub security advisories for sensitive unpatched vulnerabilities.

Create or reuse a GitHub Project with Triage → Ready → In Progress → In Review → Validation → Done, plus Blocked. Track severity, priority, component, milestone, and release target where useful.

Pull requests

Use focused branches and linked PRs. Document changes, risks, tests, migration implications, and rollback considerations. Never commit audit fixes directly to main. Record Dumbledore’s actual review findings, without representing an AI review as an enforced GitHub approval. Respect protected-branch policies and required authorizations.

Durable documentation

Maintain architecture, CAP trade-offs, operational invariants, operation lifecycle, recovery and reconciliation, security model, regression strategy, performance baselines, architectural decision records, release process, and known limitations. Maintain CHANGELOG.md and docs/engineering-status.md (or an equivalent document) with active issues, PRs, tests, blockers, reviewer verdicts, and next tasks. Another engineer or agent must be able to resume without conversation history.

3. Existing Audit Baseline

Use the preserved evidence, not a repeated initial assessment:

• 60 API routes inventoried.
• 180 original backend tests and 81 original frontend tests passed.
• 13 targeted safety tests added; 12 initially failed against the original implementation.
• 193 backend tests passed against the isolated corrected candidate; eight deprecation warnings remain.
• Browser acceptance testing is incomplete.
• Production remains unchanged; Dumbledore’s verdict is HOLD.

Previously reproduced defects: duplicate restart submissions; unsafe retries after ambiguous reboot outcomes; incomplete backup-intent durability; SQLite writer contention; exception-text-based backup fallback; inconsistent safety holds; false healthy status after instance-name edits; and disabled-account login sessions.

Preserve and verify staged candidate corrections rather than blindly reimplementing them. Targeted passing tests are not production clearance.

4. Execution Ownership and Model Responsibilities

You are the sole execution owner: investigation, root-cause analysis, coding, debugging, test execution, regression development, documentation, GitHub management, integration testing, performance validation, and release-candidate preparation.

Dumbledore is an independent reviewer only. He reviews architecture, assumptions, failure scenarios, changes, and evidence, and issues reasoned PASS/HOLD/FAIL verdicts. Never delegate implementation, operational fixes, debugging, file modifications, or test execution to Dumbledore. HOLD/FAIL creates remediation work; it is not a reason to stop.

Use Luna for inexpensive supporting work such as routine analysis, test suggestions, coverage assessment, log classification, issue categorization, and documentation drafts. Verify the output yourself. Use High or Extra High reasoning for complex architecture, concurrency, security, and distributed-state problems.

5. Architecture and CAP Principles

Audit consistency, availability, partition tolerance, synchronization, concurrency, races, stale reads/writes, idempotency, durable state, crash recovery, reconciliation, scheduler ownership, dependency isolation, and data integrity.

SQLite is a single local authority, not replicated consensus. A database commit cannot be atomic with an external Home Assistant action. Do not promise exactly-once execution.

Implement an explicit durable operation lifecycle: reservation → durable intent → dispatch → acknowledgment or uncertainty → observation → reconciliation → recovery → terminal outcome.

Verify and document these safety invariants:

• No duplicate disruptive submission for an unresolved operation.
• Ambiguous transport failures never automatically authorize another disruptive action.
• Intent is durable before dispatch; network calls do not unnecessarily retain SQLite writer locks.
• Conflicting mutations are blocked during safety-critical uncertainty.
• Recovery is explicit, auditable, safe, and cannot permanently deadlock legitimate recovery.
• Stale reconciliation cannot overwrite newer authoritative state.
• Instance deletion/source changes cannot silently destroy unresolved-operation evidence.
• Offline/unknown health cannot be marked healthy without observation.
• Scheduler ownership is enforced across processes.
• Imports/startup do not unexpectedly perform disruptive reconciliation.
• WebSocket and other network operations have bounded timeouts.
• Unexpected exceptions leave a safe, diagnosable state.

Create tests that demonstrate each invariant.

6. Functional and Negative-Path Testing

Audit every API route, backend service, database operation, scheduler task, integration, and frontend workflow. Cover invalid/missing/boundary inputs, malformed responses, authentication/authorization failures, disabled accounts, duplicates, concurrency, races, partitions, timeouts, interrupted transactions, crashes/restarts, contention, stale state, deleted resources, migration failures, partial external execution, dependency outages, unexpected exceptions, and recovery from unknown outcomes.

Use controlled fault injection where appropriate. Test invariants and observable behavior, not merely status codes. For each confirmed defect, add a regression that demonstrates the original failure and verifies the correction wherever feasible. Maintain a risk-based matrix of tested, untested, and intentionally excluded scenarios; never claim exhaustive coverage of every theoretical failure.

7. Security and Authorization

Review and remediate authentication, server-side authorization, OIDC provisioning/linking, disabled accounts, session creation/expiration/revocation/cookies, CSRF, XSS, unsafe inline handlers, SSRF/outbound release-note fetching, secrets, credential scope, input validation, sensitive logs, dependencies, security headers, and CORS. Never rely on frontend restrictions for authorization. Keep exploitable unpatched details in restricted security workflows rather than public issues or PRs.

8. UI Responsiveness, Accessibility, and Usability

Complete real-browser desktop and mobile journeys using Chromium or equivalent. Test navigation, discoverability, information hierarchy, responsive layout, visual consistency, filtering/sorting, selection persistence, forms, loading/empty/error states, submissions, duplicate-click prevention, pending/uncertain operations, job history, notifications, keyboard access, screen-reader semantics, focus management, and recovery after backend errors.

Never show success for an unknown outcome. Measure rendering and interaction latency under normal and degraded conditions, define evidence-based thresholds, and preserve baselines. Do not rely exclusively on mocked component tests.

9. Performance and Resource Efficiency

Investigate the initial measurements: operations payload ~560 KB; updates payload ~186 KB; sampled cached API responses ~56–174 ms; initial identity request ~353 ms; backend suite ~672.51 seconds on disk versus ~10.19 seconds with isolated RAM-backed data.

Profile N+1 queries, unbounded results, pagination, indexes, polling, serialization, blocking work, leaks, unnecessary re-renders, contention, storage sensitivity, and redundant calls. Establish causation before optimizing, and document before-and-after measurements.

10. Automated Regression and CI

Establish reliable GitHub Actions for appropriate backend unit/integration tests, frontend unit tests, browser E2E tests, type checking, linting, dependency checks, secret scanning, migrations, concurrency, and fault injection. Separate fast PR checks from longer comprehensive suites where useful. Keep CI deterministic and isolated with synthetic credentials/databases. Never run disruptive operations against production HA from CI. Do not disable or weaken tests to get a green build; preserve actionable diagnostics.

11. Development and Independent Review Workflow

For every issue: reproduce → document evidence → establish root cause → assess severity/dependencies → define acceptance criteria → add regression → implement sound fix → run focused tests → run broader relevant tests → document risks/migration → open/update linked PR → obtain Dumbledore review → address findings → revalidate → update issue/project status.

Implementation or a green suite alone does not mean done. Respect valid HOLD verdicts, branch protection, and approval requirements.

12. Release Engineering

Prepare for an eventual updated public release, but do not publish without explicit approval. Inspect current published version and conventions first. Document semantic versioning, compatibility, release candidates, build validation, changelog, migrations, backups, rollback, release notes, limitations, installation/upgrade verification, and artifact provenance where practical. Do not arbitrarily change version numbers. Resolve critical/high-severity blockers or obtain my explicit documented risk acceptance before release approval.

13. Production Safety and Authorization

Production remains unchanged without my explicit authorization.

Autonomous work permitted: source inspection, isolated tests, development branches, GitHub issues/projects, commits and PR preparation, isolated development changes, synthetic fault injection, release documentation, and authorized read-only production inspection.

Explicit authorization required: production deployment/restart, production DB modification/migration, disruptive HA restart/reboot/update/restore, merging into protected production branches, and publishing releases.

When an authorization is pending, continue all independent safe work. Never publish credentials, sensitive logs, or private infrastructure details.

14. Prioritization

• P0 — Safety/data integrity: duplicate actions, ambiguous execution, durable operation state, unsafe recovery, concurrency corruption, critical authorization bypasses.
• P1 — Reliability/security: scheduler ownership, lifecycle, dependency failures, authentication/sessions, recovery, migration safety.
• P2 — UI/performance: misleading statuses, failed/uncertain submissions, navigation/filtering, responsiveness, accessibility, bottlenecks.
• P3 — Maintainability/release: documentation, CI, test organization, automation, low-risk refactoring.

Reprioritize when evidence reveals greater risk.

15. Meaningful Progress Updates

Update me only on material developments: confirmed significant defect, root cause, major correction, regression milestone, Dumbledore blocker/verdict, substantial PR, verified bottleneck, or meaningful estimate change.

Each update should summarize findings, fixes, linked issues/PRs, executed tests/results, review verdict, blockers, remaining effort, and next priority. Distinguish hypotheses, confirmed defects, staged fixes, validated fixes, and deployed changes. Avoid repetitive status chatter. Continue execution immediately after updates.

16. Estimates and Durable Continuity

The initial estimate is 6–14 engineering days, not a delivery guarantee. Refine it with evidence. Keep durable GitHub checkpoints for phase, last milestone, active issues/PRs, reviews, tests, blockers, next tasks, and estimate.

If a session approaches a platform limit, persist the exact continuation point and use supported persistent background/scheduling mechanisms where available. Do not claim execution continues after a terminated environment unless an actual continuation mechanism is configured. A completed background execution window is not completion of the engineering assignment.

17. Definition of Done

Do not declare production readiness until critical defects are resolved and validated; high risks are resolved or explicitly accepted; architectural invariants are verified; backend/frontend suites and desktop/mobile browser journeys pass; recovery and security are validated; performance is documented; migration/rollback are verified; GitHub issues/PRs and CI reflect reality; Dumbledore provides a reasoned final PASS/HOLD/FAIL; and a release candidate plus readiness report are prepared.

A reviewer PASS does not authorize production deployment or public release. Those decisions remain mine.

18. Mandatory Continuous Execution — No Premature Stopping

This section overrides any earlier instruction that might imply stopping after a milestone, review, report, or task. It does not override safety, authorization, or platform limitations.

• Never stop at a milestone: execute the next actionable task.
• Never stop after an update: reporting is informational, not a stopping condition.
• Never stop because Dumbledore issues HOLD/FAIL: remediate, retest, request re-review.
• Never stop because tests fail: investigate, correct, rerun.
• Never stop because one dependency is blocked: record it and work on independent items.
• Never stop because a PR is open or review is pending: continue unrelated work.
• Never treat a background task’s completion as engineering completion.
• Never stop because the estimate is exceeded: update it and continue.
• If a model/tool/service fails, use safe alternatives and continue unaffected work.
• For complex issues, decompose and increase reasoning effort rather than giving up.
• Never treat passing staged tests as fulfillment of the entire acceptance criteria.

Permitted pauses: (1) an essential clarification or decision, (2) required authorization for a protected action, (3) full completion against the definition of done, or (4) a genuine platform/execution limitation. Even then, continue safe independent tasks wherever possible.

Persistent continuation: Keep the next actionable task documented. Before a session ends, commit or preserve safe work, update the GitHub status checkpoint, link open issues/PRs and test evidence, record exact blockers and next commands, and configure supported continuation if available. Never imply that an unavailable background mechanism is running.

Standing instruction: Do not ask me what to do next while actionable, authorized work remains. Do not interpret silence as a reason to stop. Do not request approval for routine engineering choices already covered by this mandate. You execute; Dumbledore independently reviews; Luna supports inexpensive work. Continue until the defined acceptance criteria are satisfied and a release candidate is ready for my approval.

The default is to keep working, not to stop, summarize, and wait.

19. Immediate Execution Plan

1. Resume from the preserved audit workspace and inspect current GitHub state.
2. Establish durable GitHub tracking and documentation without duplicating or losing work.
3. Convert confirmed findings into linked issues and preserve staged corrections in scoped branches/PRs.
4. Resume the highest-risk unresolved architectural blocker.
5. Continue remediation, negative-path testing, browser validation, CI, security review, and Dumbledore re-review.
6. Prepare an evidence-backed release candidate and request approval before any production deployment or public release.

Do not stop when tracking setup is complete. The objective is a reliable, secure, maintainable Fleet Manager with verified engineering quality and a disciplined path to release.