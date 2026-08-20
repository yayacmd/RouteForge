# RouteForge

**Plan a day's delivery routes across a fleet of vehicles** — time windows,
mid-route reloading, multiple products — then hand each driver a run sheet.
Built for fuel trucks, generalised for any delivery material, installs in one step.

Built on Google [OR-Tools](https://developers.google.com/optimization). It
solves a Capacitated Vehicle Routing Problem with Time Windows (CVRPTW), which
is the formal name for: *given these stops, these trucks, and these delivery
windows, what order should everyone drive in?*

Originally written for fuel delivery; it now works for anything you load onto a
vehicle and drop off — water, propane, produce, pallets, laundry, kegs.

> **About this project.** I built the original version for oil and fuel trucks —
> the routing model, the depot reloading, the delivery windows. I no longer run
> it, so rather than let it sit on a drive I rebuilt it as a web app with AI
> assistance: generalised from fuel to any delivery material, rewritten as a
> server with an API, and packaged so it installs in one step. It's MIT
> licensed — fork it and make it yours.

---

## What it does

- **Multiple vehicles**, each with its own capacity, start location, and shift hours
- **Multiple products** per vehicle and per stop — not limited to one commodity
- **Reload depots**, so a truck can refill mid-route and deliver more than it
  can carry in a single load
- **Delivery windows** that are either *required* (must be met) or *preferred*
  (late is allowed, and flagged in the results)
- **Stop priority** — must-deliver, deliver-if-possible, or optional
- **Pin a stop to a specific truck**, or hold it back from today's run
- **Plain-language explanations** when a day won't fit, instead of a bare failure
- **Run sheets** with a time rail, per-stop drops, and a load bar
- **Map** of every route, **CSV export** for printing
- **An API** so you can plan routes from a script or a nightly job
- **Installable** — add it to your desktop or phone home screen (PWA)

---

## Getting it running

Four ways, easiest first. Pick one.

### 1. Deploy to the cloud (no technical skill needed)

Gets you a private RouteForge on the internet with an HTTPS address you can
open from anywhere — the office, a phone, a warehouse tablet.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/OWNER/routeforge)
[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/template/new?template=https://github.com/OWNER/routeforge)

You'll need a free Render or Railway account. Both ask for a card even on free
plans. Render's free instances go to sleep when unused, so the first route of
the morning takes a moment to wake up — the Starter plan avoids that.

Once it deploys, open the address it gives you and the app walks you through
setup.

> Your delivery data and API key are stored on that hosting provider. If you'd
> rather keep everything on your own hardware, use option 2 or 3.

### 2. Install on a computer in your office

Needs [Docker Desktop](https://www.docker.com/products/docker-desktop/)
installed first — that's the only prerequisite.

**macOS / Linux**
```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/routeforge/main/install.sh | bash
```

**Windows** (PowerShell)
```powershell
irm https://raw.githubusercontent.com/OWNER/routeforge/main/install.ps1 | iex
```

The installer downloads RouteForge, starts it, and opens your browser. By
default it's reachable **only from that computer** — see
[Letting others reach it](#letting-others-reach-it) below.

### 3. Docker Compose

```bash
git clone https://github.com/OWNER/routeforge.git
cd routeforge
docker compose up -d
```

Open <http://localhost:8000>. Data persists in the `routeforge-data` volume.

### 4. From source

```bash
git clone https://github.com/OWNER/routeforge.git
cd routeforge
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

---

## First run

1. Open the app. It asks you to set things up once.
2. **Name your operation** — appears in the header.
3. **Choose a password.** Everyone in the office shares this one password.
   There are no individual accounts.
4. **Add a map service key.** RouteForge needs one to look up addresses and
   measure driving distances. A free key from
   [LocationIQ](https://locationiq.com/register) takes about a minute.

Until you complete setup, the instance serves *only* the setup screen — so a
fresh cloud deployment is never briefly open to whoever finds the URL.

### Then

1. **What you deliver** — name your product(s) and the unit you measure in.
2. **Places** — search addresses to add your yard, customers, and depots.
3. **Vehicles** — capacity and (optionally) shift hours per truck.
4. **Reload depots** — optional, but they let a truck deliver more than one load.
5. **Today's stops** — amounts and delivery windows.
6. **Build routes.**

In a hurry? Click **Load a demo day** on the first screen to see a worked
example immediately.

---

## Choosing a map service

| | Free tier | Rate limits | Setup |
|---|---|---|---|
| **LocationIQ** *(default)* | Yes | Tight — the app paces itself to stay under them | Paste a key |
| **OpenRouteService** | Larger | Moderate | Paste a key |
| **Self-hosted OSRM** | Unlimited | None | Download a map region, run a container |

If you plan routes every day, or you have more than a couple of dozen stops,
self-hosted OSRM is worth the setup: it removes the rate limits entirely and
makes planning much faster. See `docker-compose.osrm.yml` for a worked example.

RouteForge also **caches every distance it measures**, so re-planning a day
you've already planned costs no API calls at all.

---

## Letting others reach it

Options 2 and 3 bind RouteForge to `127.0.0.1` — the computer it's installed
on, and nothing else. That's the safe default. To let other people use it:

- **Your office network only.** In `docker-compose.yml`, change
  `"127.0.0.1:8000:8000"` to `"8000:8000"`. Everyone on the same network can
  then reach it at `http://<that-computer's-IP>:8000`.
- **From anywhere, securely.** Install [Tailscale](https://tailscale.com/) on
  the host and on each device that needs access. No ports opened, encrypted,
  and you get an HTTPS address.
- **From anywhere, public.** Use a reverse proxy with a real certificate
  (Caddy and Cloudflare Tunnel both do this well).

**Don't just forward a port on your router.** RouteForge has one shared
password and no brute-force lockout — it's built for a trusted network or a
proper proxy in front of it, not the open internet.

Note that **installing it as an app** (the desktop/home-screen shortcut)
requires HTTPS. Cloud deployments get that automatically; local installs need
Tailscale or a proxy.

---

## Using the API

Everything the web interface does is available over HTTP, so you can plan
routes from a nightly job or wire RouteForge into an existing order system.

Find your token under **Settings → Automation**. Interactive documentation is
at `/docs` on your instance.

```bash
curl -X POST https://your-instance/api/solve \
  -H "X-API-Token: YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "commodities": [{"id": "diesel", "name": "Diesel", "unit": "gallons",
                     "minutes_per_unit": 0.02}],
    "locations": [
      {"id": "yard", "name": "Main Yard",  "latitude": 39.4864, "longitude": -75.0257},
      {"id": "c1",   "name": "Acme Farm",  "latitude": 39.5100, "longitude": -75.0800}
    ],
    "vehicles": [{"id": "t1", "name": "Truck 1", "start_location_id": "yard",
                  "capacities": {"diesel": 3000}, "starting_load": {"diesel": 3000}}],
    "stops": [{"id": "s1", "location_id": "c1", "demands": {"diesel": 500},
               "window_start_minutes": 480, "window_end_minutes": 720}],
    "settings": {"day_start_minutes": 360, "effort": "normal"}
  }'
```

`/api/solve` is **self-contained**: it takes a whole problem and returns
routes, without touching anything saved in the app. Times are minutes from
midnight (8:00 AM = 480).

---

## When a day won't fit

RouteForge tries to tell you *why*, in plain terms — total demand exceeding
fleet capacity, a stop larger than any truck, a window that closes before any
shift starts. If it still can't find a plan, the usual fixes are:

- Change tight windows from **Required** to **Preferred**
- Increase the maximum shift length
- Add a vehicle, or a reload depot
- Set planning effort to **Thorough**

---

## Customising it

The vocabulary follows what you type in — name your product "Kegs" measured in
"barrels" and that's what the whole interface says.

For deeper changes, the code is deliberately small and dependency-light:

| File | What's in it |
|---|---|
| `app/models.py` | The data model and validation rules |
| `app/solver.py` | The OR-Tools optimisation |
| `app/diagnostics.py` | The plain-language feasibility explanations |
| `app/providers/` | Map service backends — add your own here |
| `app/main.py` | The HTTP API |
| `app/static/` | The whole frontend: no build step, edit and reload |

Tests:

```bash
python -m tests.test_solver     # optimiser, synthetic distances, no network
python tests/test_api.py        # API and auth, stubbed provider
python tests/test_browser.py    # full browser run (needs playwright + a server)
```

---

## Limitations

- **One dataset per instance.** No separate accounts or teams. Fine for one
  operation; if you need several, run several instances.
- **One shared password.** No per-user logins or audit trail.
- **Planning, not tracking.** No live GPS, driver check-ins, or history.
- **Route geometry needs the map service.** If it's unreachable, you still get
  routes and run sheets — just no drawn lines.

---

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, sell it, whatever helps.
