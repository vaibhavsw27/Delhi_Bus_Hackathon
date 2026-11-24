# BusPulse Delhi

Delhi's bus timetable assumes every bus moves at a constant speed. BusPulse learns how fast buses actually move from one month of real DTC bus GPS (October 2026), using about 30 million points a day. It turns that into stop-to-stop travel times for every half hour of the day.

On top of those times it offers:
- a journey planner for riders, in English and Hindi, that installs on phones;
- a live mode on Delhi's real-time bus feed;
- a dashboard for operators (DTC and the Transport Department).

On 330,803 real trips the model never saw, its average error is **5.0 minutes, against 10.5 for the timetable**: about half the error.

## How to run

Run all commands from the project folder (Windows shown).

**Data is not included in this repository.** It's too large for GitHub (the GPS archive alone is 16 GB) and belongs to the hackathon organisers. Get it from the Transport Stack Hackathon 2026 (Transport Stack / IIIT Delhi):
- the October 2026 DTC GPS archive;
- the static GTFS "Delhi Buses: GTFS Static Data - DTC / DoT" from https://delhi.transportstack.in.

Then build everything with the steps below. `data/README.md` shows where each file goes.

**Start the app.** These two terminals are all you need day to day, once the data is built.
```
# Terminal 1: the app on this laptop (open http://127.0.0.1:8000/)
.venv\Scripts\python -m uvicorn api.server:app --host 127.0.0.1 --port 8000

# Terminal 2 (optional): a temporary HTTPS link for phones; it prints the link
tools\cloudflared.exe tunnel --url http://127.0.0.1:8000
```
- Terminal 2 needs Cloudflare's free `cloudflared.exe`, which is not in this repo. Download `cloudflared-windows-amd64.exe` from https://github.com/cloudflare/cloudflared/releases, rename it to `cloudflared.exe`, and put it in `tools/`.
- Both work only while the laptop is on and awake, online, and both terminals are open.
- The phone link changes every time Terminal 2 restarts.
- On restrictive Wi-Fi, add `--protocol http2` before `--url`.
- Live buses need about 2 minutes after start-up to fill in.

**Rebuild everything from the raw data.** This is only needed once, or after code changes to the pipeline.
1. Install Python 3.11-3.14 packages:
   ```
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```
2. Place the data:
   - `data/raw/delhi-dtc-bus-gtfs-rt-2026-10.7z`: the GPS archive.
   - `data/raw/gtfs/*.txt`: the static GTFS "Delhi Buses: GTFS Static Data - DTC / DoT".
   - Optional: `secrets/otd_api_key.txt`, a free key from otd.delhi.gov.in/data/realtime, for live mode. Never commit or share it.
3. Run in order. The GPS pipeline takes about 1 hour, is resumable, and logs to `logs/pipeline.log`.
   ```
   .venv\Scripts\python -m pipeline.extract
   .venv\Scripts\python -m pipeline.m0_inspect
   .venv\Scripts\python -m pipeline.gtfs_network
   .venv\Scripts\python scripts\run_pipeline.py --dates all
   .venv\Scripts\python -m pipeline.build_model
   .venv\Scripts\python -m pipeline.build_live
   .venv\Scripts\python validation\validate.py
   .venv\Scripts\python -m pipeline.build_ops
   ```
4. Run the checks with `.venv\Scripts\python scripts\check.py m0`, and likewise `m1` to `m5`. Each prints `PASS` or `FAIL`.

To see changes: refresh the browser after web changes (`web/`). Restart Terminal 1 after Python changes; the phone link keeps working.

## What the app does

- **Home screen.** A map of Delhi with live buses gliding behind four tiles: Live buses, Plan a trip, City traffic, For operators. Quick trips (Home → Work) appear once places are saved.
- **Plan a trip.** Pick any two places (stop search, a tap on the map, or "My location"), today or up to 90 days ahead, leaving at or arriving by any time. The answer is up to 3 bus options. Each shows:
  - the total time and the arrival time ("usually there by" covers late days);
  - a reliability dot and "Traffic adds N min";
  - a tip when leaving at another time is clearly faster;
  - a graph of the trip's time through the day;
  - the route coloured by bus speed.
- **Live.** Today's real DTC feed: real buses on the map, and trip times adjusted to how buses are moving right now. On 27 Oct 2026 it plays back recorded GPS instead, clearly labelled as a replay.
- **City traffic.** Every bus road in Delhi coloured by usual bus speed at the chosen time.
- **Saved places.** Home and Work are stored only on the device.
- **For operators (`/ops`).**
  - City bus speed through the day.
  - The rush-hour chokepoints that cost the most bus-minutes.
  - The routes whose timetable is furthest from reality, with a downloadable corrected timetable.
  - The least predictable routes, and bunching.

## How the model works

- **Segments.** A segment is one consecutive stop pair from the static GTFS (14,971 segments), shared by every route that drives it.
- **Cleaning.** The pipeline removes duplicates (39% of GPS rows), GPS spikes and parked-bus points.
- **Matching.** Each GPS point is snapped onto its own route's line. Forward-moving stretches become runs, and stop-crossing times are interpolated. Dwell and signal time stay inside the segment time; terminal layovers are excluded.
- **Estimates.** Each segment has an estimate per day type (weekday, Saturday, Sunday/holiday) and 30-minute bucket. It is the trimmed mean (10th-90th percentile) of the observed times.
- **Fallback.** Sparse cells fall back through five levels:
  - L1: the cell itself (31% of cells);
  - L2: neighbouring half hours (15%);
  - L3: the segment average × a citywide daily profile (42%);
  - L4: route speed (7%);
  - L5: the timetable (5%).
  Each estimate keeps its level, and that drives the confidence label.
- **Calibration.** One factor, k = 1.013, makes predicted trip times match actual ones on training data.
- **Reliability.** "Usually there by" adds the training-day 90th-percentile lateness for that hour, plus waiting risk.
- **Live.** Every minute, recent observed / expected times per segment are blended toward the citywide ratio. The effect fades out over 30 minutes ahead.
- **Planner.** RAPTOR (numba) with up to 3 buses, time-dependent hop times, half-headway waits capped at 12 minutes, and transfer walks under 300 m. A query takes about 20-50 ms.

## Validation

Trained on 1-24 October and tested on 25-31 October. Every test-day stretch of at least 10 observed stops is scored. The baseline is the GTFS timetable.

| In-bus time, 330,803 test trips | BusPulse | Timetable |
|---|---|---|
| Average error (MAE) | **5.03 min** | 10.48 min |
| Error as % of trip (MAPE) | **11.0 %** | 23.2 % |
| Bias | -1.11 min | -2.63 min |

- MAE is 52% lower than the timetable's. The model beats the timetable on all 20 fixed-rule routes in `validation/od_table.md`, for example MC715UP (2.2 vs 6.9 min) and 546STL-GHD-IIDOWN (3.7 vs 10.9 min).
- On 27 Oct, live replay factors cut MAE from 4.49 to 4.18 min.
- "Usually there by" held for 85% of test trips. The app therefore says "usually", not "9 in 10".
- The model underestimates busy afternoons on the test week (about 3 min short at 17:00).
- Details: `validation/results.json`, `validation/accuracy_slide.png`. The Google Maps comparison is manual: fill `validation/google_comparison.csv`, then run `validation\google_template.py score`.

## Limits

- Bus only (no Metro). Walks are straight lines × 1.3 at 1.2 m/s. Waits are half the timetable headway, not measured.
- Learned from October 2026 only. Later months' holidays and seasonal changes are not known; live mode partly corrects for this today.
- Only in-bus time is validated against GPS. Walk and wait times are modelled.
- The GTFS timetable is a constant-speed estimate. Segments with no GPS (5% of cells) fall back to it, and their confidence label says so.
- Everything runs on one laptop. A phone needs the laptop running plus the HTTPS tunnel.
- Data quirk handled: GPS route ids are stored as `837.0` while GTFS uses `837`, so route ids are normalised before joining.

## Next steps

- Keep learning from the live feed every day, so new months, festivals and holidays are covered.
- Next-bus arrival times at every stop.
- Crowding and fares from the ONDC ticket data.
- Bus + Metro journeys.
- Free permanent hosting, so the app works without the laptop.
