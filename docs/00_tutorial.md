# End-to-end tutorial: Fabric (Medallion) → Foundry (Agents) → Dashboard

This tutorial builds a minimal industrial anomaly POC:

- **Vision modality:** evidence frames extracted from conveyor videos
- **Telemetry modality:** synthetic PLC channels (temperature + current + vibration + speed + feed rate)
- **Gold-only contract:** Foundry reads **Gold** casefiles and writes findings back for dashboards

---

## 0) What you will build

**Bronze (raw landing)**
- Raw videos uploaded to OneLake
- Labeled anomaly frames (JPGs) uploaded to OneLake
- `bronze_video_index` table: list of videos + paths

**Silver (curated)**
- Extract sampled frames (default: every 30 frames ≈ 4 fps at 120 fps)
- `silver_frame_index`: one row per extracted frame with timestamps
- `silver_label_frame_index`: parses dataset label filenames into (video_id, frame_number, ts_ms, label)
- `silver_event_windows`: groups anomaly label frames into event windows
- `dim_material_thermal_profile`: expected temperature bands per material
- `silver_plc_readings`: synthetic PLC readings (10 Hz)
- `silver_plc_event_summary`: aggregated PLC features per event (JSON)

**Gold (Foundry-ready casefiles)**
- `gold_conveyor_event_fact`: per-event casefile with evidence frames + PLC summary
- `gold_conveyor_event_fact_flat`: flattened features for dashboards / Data Agent
- `gold_latest_run` view: deterministic “latest run” helper for the Data Agent

---

## 1) Create folders & upload data

In your Lakehouse create:

- `Files/bronze/videos/conveyer/raw_mp4/`  (upload MP4/MKV videos)
- `Files/bronze/labels/conveyer/anomaly/`  (upload labeled anomaly JPGs)
- `Files/bronze/labels/conveyer/normal/`   (optional)

Keep dataset label filenames. Example:
`10754-boardcamera_iron_ore_jaspilito_detected_1.jpg`

---

## 2) Run Bronze notebook
Open `notebooks/01_bronze_notebook.py` in Fabric Notebook and run all.

Outputs:
- `bronze_video_index` Delta table

If it finds 0 videos, your upload path doesn’t match. Fix the folder or notebook config.

---

## 3) Run Silver notebook
Open `notebooks/02_silver_notebook.py` and run all.

Outputs:
- Extracted frames in `Files/silver/frames/conveyer/frames_4fps/<video_id>/...`
- Silver tables (`silver_frame_index`, `silver_label_frame_index`, `silver_event_windows`, PLC tables)

If OpenCV cannot open videos, the notebook copies videos to `/tmp` and reads locally.

---

## 4) Run Gold notebook
Open `notebooks/03_gold_notebook.py` and run all.

Outputs:
- `gold_conveyor_event_fact`
- `gold_conveyor_event_fact_flat`
- `gold_latest_run` view

Gold “snaps” label frame numbers to the nearest extracted frame number if you sampled every N frames.

---

## 5) Configure Data Agent (optional)
Use `docs/10_data_agent_config.md`.

---

## 6) Configure Foundry Agents
Use `docs/20_foundry_agent_configs.md`.

For true multimodal, your Visual Analyst should receive images via a tool (OpenAPI FrameFetcher).

---

## Troubleshooting

### Data Agent says 0 events
Verify in a Fabric notebook:

```python
spark.sql("SELECT COUNT(*) AS n FROM gold_conveyor_event_fact_flat").show()
spark.sql("SELECT run_id, COUNT(*) AS n FROM gold_conveyor_event_fact_flat GROUP BY run_id ORDER BY n DESC").show(50, truncate=False)
```

If those are 0, rerun Gold notebook.

### OpenCV: could_not_open
Ensure the file is local (`/lakehouse/default/Files/...`) or copy to `/tmp` first (Silver notebook does this).

### Labels vs extracted frames mismatch
Expected when sampling every N frames. Gold notebook “snaps” to nearest extracted frame.

