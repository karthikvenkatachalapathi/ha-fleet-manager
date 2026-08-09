# Building Home Assistant Fleet Manager

I built Home Assistant Fleet Manager because my Home Assistant setup stopped being a single-home problem.

I started with one Home Assistant instance for my primary residence. Over time, there were other places I cared about too: a parents' home, rental properties, and remote locations where Home Assistant gives useful visibility and control.

Each instance worked fine on its own. The friction was maintenance.

When something needed attention, I had to log into each Home Assistant instance separately, check updates, look for repairs, confirm whether anything had been skipped, and remember what I had already handled. That is manageable with one home. It gets repetitive and error-prone when there are several.

Fleet Manager is the small control layer I wanted in front of that reality.

## The problem

Home Assistant is excellent at managing one environment. But if you run more than one instance, simple questions become manual:

- Which homes have pending updates?
- Which updates were skipped?
- Are there Repairs waiting somewhere?
- Did I already restart or back up that instance?
- Which property needs attention first?

The default answer is usually: open every instance, one at a time.

That works until it does not.

For a primary residence, a missed repair can be annoying. For a parents' home or a rental property, it can mean driving blind into a problem that could have been caught earlier. I wanted one page that showed fleet health first, then let me drill into the specific instance that needed attention.

## What I built

Home Assistant Fleet Manager is a self-hosted web app that sits outside Home Assistant and talks to each configured instance through the Home Assistant API.

It gives one place to:

- see pending updates across all instances
- filter by instance, update type, risk level, or skipped status
- run or skip updates
- see Home Assistant Repairs
- trigger backups and restarts
- keep recent activity and audit history
- configure notifications for things that need attention

Stored Home Assistant tokens stay on the server. The browser only gets sanitized data, and the backend owns the calls to each Home Assistant instance.

## Why centralize it?

The goal was not to replace Home Assistant. I still use the normal Home Assistant UI for each home.

The goal was to reduce repeated checking.

When you manage multiple homes, the most useful screen is not always the most detailed one. It is the one that answers: "Where do I need to look right now?"

A centralized view makes that possible:

```text
Primary residence     pending: 0   skipped: 1   repairs: 0
Parents' home         pending: 2   skipped: 0   repairs: 1
Rental property A     pending: 0   skipped: 3   repairs: 0
Rental property B     pending: 1   skipped: 0   repairs: 0
```

That is much easier than logging into four separate dashboards just to learn the same thing.

## Design choices

I kept the app intentionally small:

- FastAPI backend
- SQLite for a simple self-hosted install
- single browser UI
- Docker or native Python deployment
- Home Assistant API calls only; no generic proxy
- password login with optional OIDC

The UI is organized around a few practical pages:

- **Fleet health**: update status across all instances
- **Repairs**: Home Assistant repair issues that need attention
- **Recent activity**: what changed recently
- **Settings**: instances, notifications, account, OIDC, and system options

I also wanted summary cards to be useful. If a card says Skipped, clicking it should show skipped updates. If it says Instances, it should open the instance configuration list. Summary numbers should be entry points, not decoration.

## What changed for me

The biggest improvement is mental overhead.

Before, checking several homes meant context switching across several Home Assistant URLs. After centralizing it, the workflow is:

1. Open Fleet Manager.
2. Check fleet health.
3. Drill into only the instance that needs attention.
4. Update, skip, restart, or back up from one place.
5. Move on.

That is a small change, but it removes a lot of repeated work.

It also makes the project easier to explain. Another person reviewing the repo does not need to understand my entire home setup. They only need to understand the pattern: multiple Home Assistant instances, one central maintenance view.

## Why this matters beyond my setup

A lot of home automation starts as a hobby and slowly becomes infrastructure.

Once it supports a primary residence, family homes, rentals, or remote properties, the problem changes. It is no longer only about automations. It is about maintenance, visibility, credentials, audit history, and knowing what needs attention without logging into every instance manually.

That is the gap Fleet Manager tries to fill.

It is not a replacement for Home Assistant. It is a small maintenance layer for people who already run more than one Home Assistant instance and want a cleaner way to manage them.

Repo: https://github.com/karthikvenkatachalapathi/ha-fleet-manager
