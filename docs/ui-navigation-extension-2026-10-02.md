# UI/navigation extension — 2026-10-02

## Added acceptance scope

- Audit and improve hierarchy, labels, navigation, and responsive layout without removing existing behavior.
- Overview must surface instance health, updates, repairs, active operations, failures, stale/unavailable data, and the latest trustworthy refresh timestamp.
- Preserve context across overview → instance → update/repair → operation logs: filters, selection, scroll, and back destination.
- Make bulk selection/action intent, active progress, and per-item outcome discoverable.
- Keep running operations visible across navigation and page reload.
- Add useful search/filter/sort plus explicit loading, empty, failed, unavailable, and reconnecting recovery states.
- Maintain accessible labels, readable contrast, ≥44 px touch targets, fixed mobile chrome, and no horizontal scrolling.
- Validate rendered desktop and mobile journeys and capture before/after screenshots.

## Safety boundary

No live Home Assistant update, repair, backup, reboot, or restart may be triggered without Karthik's explicit approval. Rendered journey validation uses an isolated local database and mocked/simulated target responses.
