# Conveyor Multimodal Contamination Ops (Fabric Lakehouse → Medallion → Data Agent → Foundry → EOD Dashboard)

**Multimodal anomaly detection POC (Vision + PLC telemetry, incl. temperature)**

This POC ingests **industrial conveyor belt videos** (iron ore flow) into a **Microsoft Fabric Lakehouse**, processes them through a **medallion architecture (Bronze → Silver → Gold)**, and enables downstream analysis via:

- **Fabric Lakehouse tables** (Bronze/Silver/Gold)
- **Fabric Data Agent** (natural-language SQL over Gold tables)
- **Azure AI Foundry agent(s)** that can use the Data Agent (when in the same tenant) to generate narrative findings
- **Power BI / Fabric dashboard** for end‑of‑day ops reporting

The “multimodal” part is *system-level*: the dataset is primarily images/video, and we add an independent PLC-like telemetry modality (simulated) aligned to video time, so agents can fuse *what we saw* with *what we measured*.

---

## Dataset + attribution

**In-lab Image Dataset of Foreign Objects and Anomalies in Iron Ore Conveyor Belts** (Mendeley Data, v1)  
DOI: **10.17632/s25x2bnshz.1** | License: **CC BY 4.0**

To download the dataset:

```bash
python scripts/download_large.py --url https://prod-dcd-datasets-cache-zipfiles.s3.eu-west-1.amazonaws.com/s25x2bnshz-1.zip --out conveyer_dataset.zip --parts 64 --chunk-mb 8
```

**Minimal dataset input**:
- `Original-raw-videos/` → Fabric Lakehouse `Files/bronze/videos/conveyor/raw_mp4/`

- `Image-files-manual-split/` (has `normal/` + `anomaly/`) → `Files/bronze/labels/conveyor/`

> If you only upload the `anomaly/` frames (or only anomalies exist), you will only get anomaly events in Gold.  
> To see normal events too, you must provide *normal* frames (or enable synthetic normal window generation in Silver—see below).

---

## What the pipeline produces

### Bronze (raw landing + inventory)
- Raw MP4s land in the Lakehouse `Files/bronze/...`
- `dbo.bronze_video_index` provides stable inventory/metadata (video_id, path, fps, etc.)

### Silver (alignment + features)
Silver turns raw media into:
- a **frame index** (`dbo.silver_frame_index`) with a timestamp per extracted frame
- **label frame index** (`dbo.silver_label_frame_index`) parsing the manual-split JPG filenames
- **event windows** (`dbo.silver_event_windows`) for both anomalies and normals (label_prior ∈ {0,1})
- **PLC-like telemetry streams** (`dbo.silver_plc_readings`) aligned by `video_id, ts_ms`
- **event-level PLC features** (`dbo.silver_plc_event_summary`) aggregated per event
- a **thermal profile dimension table** (`dbo.dim_material_thermal_profile`) used for plausible temperature signatures

### Gold (casefiles for agents + dashboards)
Gold creates one row per event window:
- `dbo.gold_conveyor_event_fact` (JSON fields for agent-friendly payloads)
- `dbo.gold_conveyor_event_fact_flat` (flattened numeric columns for Data Agent / dashboards)

Gold rows include:
- `run_id` (lineage: which pipeline run created these rows)
- `schema_version` (to evolve the contract safely)
- `label_prior` (0 = normal prior, 1 = anomaly prior)
- `plc_features_json` and/or flattened PLC feature columns
- `evidence_frames_json` (paths + timestamps for evidence images)

---

## Lakehouse folder layout (recommended)

Create these folders under **Lakehouse → Files**:

```text
Files/
  bronze/
    videos/conveyor/raw_mp4/
    labels/conveyor/     
  silver/
    frames/conveyor/frames/
  gold/
```

---

## Notebooks (the runnable pipeline)

You should have three notebooks:

1) **01_bronze.ipynb**
2) **02_silver.ipynb**
3) **03_gold.ipynb**

> Run order matters: Bronze → Silver → Gold.

### Notebook 01 — Bronze
**Goal:** inventory the videos and standardize paths.

Outputs:
- `dbo.bronze_video_index`

**Path note (common issue):**
- The Fabric UI shows paths like `Files/...`, but some libraries (e.g., OpenCV `VideoCapture`) require a filesystem path like:
  - `/lakehouse/default/Files/...`

### Notebook 02 — Silver
Silver is where “normal vs anomaly” and “label alignment” are decided.

#### 02.A Frame extraction → `dbo.silver_frame_index`
- Extract every Nth frame from each MP4
- Write `Files/silver/frames/conveyor/frames/{video_id}/frame_0000123.jpg`
- Create `dbo.silver_frame_index(video_id, frame_number, ts_ms, frame_path)`

#### 02.B Label frame indexing → `dbo.silver_label_frame_index`
Manual-split frames are “flat” (all JPGs in one folder) and named like:

```text
10754-boardcamera_iron_ore_jaspilito_detected_1.jpg
```

We parse:
- `frame_number` from the leading digits (e.g., `10754`)
- `video_id` from the middle section before `_detected_...`

and store:
- `label_prior` = 1 for anomalous folder
- `label_prior` = 0 for normal folder (if present)

**Important:** if the `normal/` folder is missing or unreadable, the pipeline will proceed with anomalies only—this is exactly why Gold might show only anomaly events.

#### 02.C Event window creation → `dbo.silver_event_windows`
**Goal:** build short time windows likely containing:
- anomalies (from anomalous labels)
- normal baselines (from normal labels, or synthetic sampling)

We support two paths:

**Path 1 (preferred): event windows from labeled frames**
- Group label frames into windows per `video_id` using a gap threshold.
- Create:
  - `label_prior = 1` windows from anomalous label frames
  - `label_prior = 0` windows from normal label frames

**Path 2 (fallback): synthetic normal windows**
If you do not have `normal/` labeled frames, generate a small number of normal windows per video by sampling timestamps **not overlapping** anomaly windows.

**Output schema**
- `dbo.silver_event_windows(event_id, video_id, t_start_ms, t_end_ms, label_prior)`

#### 02.D PLC simulation + features
- Generate `dbo.silver_plc_readings` at ~10 Hz (`ts_ms` 0,100,200,...) per `video_id`
- Aggregate into `dbo.silver_plc_event_summary` per `event_id`

---

### Notebook 03 — Gold
**Goal:** produce agent/dash-ready “casefiles” for *all* event windows (both anomaly + normal).

Gold has two key steps:

#### 03.A Evidence frames
We build evidence differently for anomalies vs normals.

**Anomaly evidence**
- Join label frames (anomalous) to the anomaly event windows they fall within.
- Then **snap** each labeled frame to the nearest extracted frame from `silver_frame_index`.  
  (This avoids assuming the label frame cadence matches your extraction cadence.)

**Normal evidence**
- For each normal window, pick “midpoint” or uniformly sampled frames from `silver_frame_index`.  
  (Normal windows typically do not have label-frame anchors.)

Both produce a JSON array like:
```json
[
  {"frame_number": 1234, "ts_ms": 10283.3, "frame_path": "Files/silver/frames/.../frame_0001234.jpg"},
  ...
]
```

#### 03.B Build Gold tables (+ run_id)
Gold writes two tables:

1) `dbo.gold_conveyor_event_fact` (agent-friendly JSON fields)
2) `dbo.gold_conveyor_event_fact_flat` (flattened numeric fields for the Data Agent)

**run_id**
- A run_id is created once per Gold notebook execution, typically:
  - `run_yyyyMMdd_HHmmss`
- This enables “latest run” questions and reproducible dashboards.

**Important:** if you overwrite the Gold table each run, you will only ever see one run_id.  
If you want multiple run_ids retained, switch writes to `mode("append")`.

---

**Quick validation:**
```sql
SELECT label_prior, COUNT(*) AS n
FROM dbo.silver_event_windows
GROUP BY label_prior;
```

Expected for a balanced demo: counts for both `label_prior=0` and `label_prior=1`.

---

## Fabric Data Agent setup (over Gold)

The Data Agent works best with `dbo.gold_conveyor_event_fact_flat` because the columns are scalar (not nested JSON).

Recommended tables to select:
- `dbo.gold_conveyor_event_fact_flat`
- `dbo.dim_material_thermal_profile` (optional, helps answer “what does this temp mean?”)

**Sanity check prompts**
- “Show me all distinct run_id values”
- “How many events are in the latest run_id?”
- “How many are label_prior=1 vs label_prior=0 in the latest run_id?”

If answers look stale or wrong:
- Re-open the Data Agent editor, confirm the correct Lakehouse is selected
- Re-select the tables (or remove + re-add) to refresh schema visibility

---

## Foundry agent(s) + Data Agent as a tool

- If Foundry and Fabric are in the **same tenant**, you can attach the Fabric Data Agent as a tool and have the Foundry agent call it to fetch event summaries.

---

## Dataset citation
> In-lab Image Dataset of Foreign Objects and Anomalies in Iron Ore Conveyor Belts (Mendeley Data), Version 1, DOI: 10.17632/s25x2bnshz.1, CC BY 4.0
