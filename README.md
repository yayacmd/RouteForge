# Route Planner

A desktop GUI tool for optimizing **multi-vehicle delivery routes** with time
windows and mid-route restocking. It solves a Capacitated Vehicle Routing
Problem with Time Windows (CVRPTW) using Google [OR-Tools](https://developers.google.com/optimization),
geocodes and routes addresses via [LocationIQ](https://locationiq.com/), and
renders the result as an interactive [Folium](https://python-visualization.github.io/folium/)
map embedded in the app.

It was originally built for fuel/oil tanker delivery and has been generalized
to handle **any two-commodity delivery operation** — beverages, water, propane,
parcels, etc. Edit the labels at the top of `app.py` to match your products.

## Features

- Multiple vehicles, each with its own start location and per-commodity capacity
- Two independent commodities tracked per vehicle and per stop
- **Restock depots**: vehicles can return mid-route to reload, so total
  delivery can exceed a single vehicle's capacity
- Per-stop delivery time windows and a configurable shift length
- Service time modeled from a loading/unloading rate plus depot restock time
- Address autocomplete (LocationIQ search) for entering locations
- Save/load route templates
- Optional: dispatch each route to a driver as a Google Maps navigation link
  via email-to-SMS gateways (US carriers)

## Requirements

- Python 3.10–3.12
- A free LocationIQ API key (the free tier is rate-limited, which is why the
  app paces its requests)

## Setup

```bash
git clone <your-repo-url>
cd route-planner
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

On first launch, paste your LocationIQ API key into the field in the top
toolbar and click **Save** (it's written to `api_key.txt`, which is gitignored).

## Typical workflow

1. **Delivery Locations** — search an address, name it, add it.
2. **Start and Depot Locations** — add vehicle start points and restock depots.
3. **Vehicles** — define vehicles and their per-commodity capacities.
4. **Route Stops** — add the stops for today's run (amounts + time windows),
   then add the vehicles available for the run with their starting load.
5. **Drivers** — optional, only needed for the dispatch feature.
6. **Build Routes** — set shift start, service rate, and max shift length, then
   **Run Routing Tool**. Routes, totals, and a map appear below.

## Customizing for your commodity

Open `app.py` and edit the config block near the top:

```python
APP_TITLE = "Route Planner"
PRODUCT_1_LABEL = "Product 1"          # e.g. "Diesel"
PRODUCT_2_LABEL = "Product 2"          # e.g. "Gasoline"
UNIT_LABEL = "units"                   # e.g. "gallons"
DEPOT_SERVICE_TIME_SECONDS = 2700      # restock time at a depot
```

If you only deliver one commodity, set the second capacity/amount to `0`
everywhere.

## Optional: sending routes to drivers

This feature emails a Google Maps navigation link to each driver's phone via
their carrier's email-to-SMS gateway. It reads SMTP credentials from
environment variables (never hardcode them):

```bash
export ROUTING_SMTP_HOST=smtp.gmail.com   # default
export ROUTING_SMTP_PORT=587              # default
export ROUTING_SMTP_USER=you@example.com
export ROUTING_SMTP_PASS=your_app_password
```

For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833),
not your account password.

## Data files

App state lives in `appdata/*.json` (locations, vehicles, stops, drivers) and
templates in `appdata/route_templates/`. Empty templates ship with the repo so
the app runs out of the box. If you'd rather not track your own data, uncomment
the relevant lines in `.gitignore`.

## Notes & limitations

- The optimizer runs for a 1-second time limit by default
  (`search_parameters.time_limit.FromSeconds(1)` in `solve_routes`). Increase it
  for larger problems.
- LocationIQ free-tier rate limits apply; the app sleeps between matrix/route
  calls to stay under them.
- The carrier SMS gateways are US-specific.

## License

MIT — see [LICENSE](LICENSE).
