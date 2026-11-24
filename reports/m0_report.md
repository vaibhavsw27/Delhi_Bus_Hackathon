# Milestone 0 inspection report

- WARNING: route proximity 0.493 is below 0.60; max_offset_m = 150

Run time: 353 s. Sample days: ['2026-10-06', '2026-10-11', '2026-10-27'].

## Decisions

| Check | Measured | Decision |
|---|---|---|
| M0-B | table vehicle_feed; map {'vehicle_id': 'vehicle_id', 'lat': 'lat', 'lon': 'lng', 'ts': 'timestamp', 'route_id': 'route_id', 'trip_id': 'trip_id', 'speed': 'speed', 'date_ist': 'date', 'time_ist': 'time'} | sqlite_reader = duckdb |
| M0-C | median rows 29,396,065 | dropped days [] |
| M0-A | 31 usable dates | train 24, test 7, replay 2026-10-27, dev 2026-10-06 |
| M0-M | counts {'stops.txt': 6862, 'routes.txt': 2574, 'trips.txt': 80357, 'stop_times.txt': 2737136, 'shapes.txt': 965607, 'calendar.txt': 1}, shape coverage 1.000, service ids 1 | use_shapes True, service_filter None |
| M0-E | date match [0.9999, 0.9999, 1.0] | ts_unit s, ts_source ts |
| M0-G | median gap 11.0 s, p90 22.0 s | run_gap_s 300 |
| M0-H | trip join rate 0.0000 | trip_id_usable False |
| M0-D | route_id null rate 0.0000 | backfill_route_from_trip False |
| M0-I | rung rates {"route_id": 0.9996, "route_short_name": 0.0, "route_long_name": 0.0} | route_key route_id (rate 0.9996); keys normalised with ROUTE_NORM_SQL (837.0 -> 837) |
| M0-N | median proximity 0.493 over 20 routes | max_offset_m 150 |
| M0-J | median reported/derived 0.901 | speed_unit m/s (not used, D15) |
| M0-K | bbox share 0.9992 | swap_latlon False |
| M0-L | est bytes 869,069,572 | day_chunks 1 |

## Deviation

GPS `route_id` is float text (`837.0`); GTFS `route_id` is `837`. Plain trimmed, upper-cased matching gives 0% on every rung, so every route join normalises whole numbers to integers first (`config.ROUTE_NORM_SQL`).

## Rows per day

- 2026-10-01: 31,290,299
- 2026-10-02: 29,316,111
- 2026-10-03: 31,516,372
- 2026-10-04: 30,213,040
- 2026-10-05: 30,991,505
- 2026-10-06: 30,857,544
- 2026-10-07: 25,535,790
- 2026-10-08: 30,213,919
- 2026-10-09: 29,099,431
- 2026-10-10: 26,763,426
- 2026-10-11: 25,936,964
- 2026-10-12: 26,210,474
- 2026-10-13: 28,695,465
- 2026-10-14: 28,847,932
- 2026-10-15: 29,271,900
- 2026-10-16: 29,043,138
- 2026-10-17: 30,024,953
- 2026-10-18: 28,848,015
- 2026-10-19: 29,270,257
- 2026-10-20: 27,362,204
- 2026-10-21: 30,243,480
- 2026-10-22: 30,971,604
- 2026-10-23: 31,377,176
- 2026-10-24: 31,327,972
- 2026-10-25: 29,396,065
- 2026-10-26: 29,972,620
- 2026-10-27: 31,038,199
- 2026-10-28: 31,359,638
- 2026-10-29: 28,264,904
- 2026-10-30: 28,643,700
- 2026-10-31: 30,972,964

## Sample days

### 2026-10-06
- ts range 1791223321 .. 1791311384, file-date match 0.9999
- duplicates 0.3884, median gap 11.0 s, p90 gap 22.0 s
- bbox share 0.9993, speed ratio 0.9006175443512829
- vehicles 6,269, routes 2,197
- route rung matches {'route_id': 30854394, 'route_short_name': 0, 'route_long_name': 0, 'via_trip_id': 0} of 30,857,544 rows
- quietest 3 h window starts 01:00 IST
- hourly rows (IST 00..23): [279222, 234413, 225739, 219952, 248184, 395362, 924102, 1483753, 1889909, 2028746, 2060451, 2051323, 1829197, 1489367, 1345594, 1351820, 1552727, 1847797, 2026634, 2056113, 1977961, 1640870, 1112894, 585414]

### 2026-10-11
- ts range 1791655320 .. 1791743374, file-date match 0.9999
- duplicates 0.3369, median gap 11.0 s, p90 gap 22.0 s
- bbox share 0.9992, speed ratio 0.6895037092935208
- vehicles 5,632, routes 1,853
- route rung matches {'route_id': 25926577, 'route_short_name': 0, 'route_long_name': 0, 'via_trip_id': 0} of 25,936,964 rows
- quietest 3 h window starts 01:00 IST
- hourly rows (IST 00..23): [178522, 143857, 136632, 126042, 158445, 337681, 811007, 1312243, 1676119, 1802961, 1828178, 1790459, 1538400, 1176286, 1048551, 1120197, 1391962, 1645763, 1765178, 1749074, 1634597, 1300963, 845528, 418319]

### 2026-10-27
- ts range 1793038630 .. 1793125783, file-date match 1.0000
- duplicates 0.4361, median gap 11.0 s, p90 gap 22.0 s
- bbox share 0.9992, speed ratio 0.9833296764072249
- vehicles 6,295, routes 2,237
- route rung matches {'route_id': 31019266, 'route_short_name': 0, 'route_long_name': 0, 'via_trip_id': 0} of 31,038,199 rows
- quietest 3 h window starts 01:00 IST
- hourly rows (IST 00..23): [217643, 161726, 153592, 149542, 187040, 404715, 972368, 1525570, 1925872, 2067413, 2097898, 2111494, 1882613, 1514153, 1350275, 1391650, 1628339, 1936738, 2059229, 2075978, 1974262, 1626697, 1088925, 534467]

## Null rates (mean of sample days)

- vehicle_id: 0.0000
- lat: 0.0000
- lon: 0.0000
- ts: 0.0000
- route_id: 0.0000
- trip_id: 0.0000
- speed: 0.0000
- date_ist: 0.0000
- time_ist: 0.0000

## Route proximity (M0-N)

- route 1315: 0.483
- route 1319: 0.505
- route 1327: 0.422
- route 1411: 0.580
- route 1625: 0.467
- route 2172: 0.500
- route 2181: 0.485
- route 2228: 0.552
- route 2395: 0.518
- route 2417: 0.649
- route 2862: 0.369
- route 3008: 0.298
- route 3048: 0.500
- route 3075: 0.316
- route 3112: 0.257
- route 3148: 0.574
- route 3244: 0.349
- route 3655: 0.520
- route 3686: 0.720
- route 853: 0.321

## GTFS (M0-M)

- counts {'stops.txt': 6862, 'routes.txt': 2574, 'trips.txt': 80357, 'stop_times.txt': 2737136, 'shapes.txt': 965607, 'calendar.txt': 1}
- null arrival rate 0.0000, max arrival hour 28
- calendar present True, distinct service ids 1
