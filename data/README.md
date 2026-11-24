# Data folder (not included in the repository)

The data is not published here. It's far too large for GitHub, and it belongs to the Transport Stack Hackathon 2026 organisers (Transport Stack / IIIT Delhi). Put the files in these places, then follow "How to run" in the main README.

```
data/
  raw/
    delhi-dtc-bus-gtfs-rt-2026-10.7z   GPS archive from the hackathon (16 GB)
    gtfs/                              static GTFS .txt files ("Delhi Buses: GTFS Static
                                       Data - DTC / DoT", https://delhi.transportstack.in)
    sqlite/                            created by: python -m pipeline.extract (116 GB)
  clean/
    positions/                         created by scripts/run_pipeline.py (about 5 GB)
    seg_obs/                           created by scripts/run_pipeline.py (about 0.4 GB)
  model/                               created by gtfs_network, build_model, build_live,
                                       build_ops (about 44 MB): what the app loads
  tmp/                                 DuckDB scratch space
```

For live mode, put a free API key from https://otd.delhi.gov.in/data/realtime in `secrets/otd_api_key.txt`. That folder is never committed.
