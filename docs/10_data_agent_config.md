# Fabric Data Agent configuration (for this POC)

## Table selection
Select ONLY:

- `dbo.gold_conveyor_event_fact_flat` (primary)
- `dbo.gold_conveyor_event_fact` (optional, for JSON)
- `dbo.dim_material_thermal_profile` (optional)

Avoid silver/bronze tables unless you explicitly want those details.

## Instructions to paste
```markdown
You answer questions about conveyor contamination events and PLC telemetry.

Use these tables in priority:
1) dbo.gold_conveyor_event_fact_flat (primary)
2) dbo.gold_conveyor_event_fact (only for JSON fields)
3) dbo.dim_material_thermal_profile (temperature expectations by material)

Definitions:
- run_id: pipeline run identifier
- event_id: event window id
- label_prior=1 means anomaly-labeled window

Rules:
- Prefer gold_conveyor_event_fact_flat unless explicitly asked for JSON.
- For "latest run", use dbo.gold_latest_run to determine run_id.
- Never claim you visually inspected frames; only return evidence paths.

Answer format:
- Include the result table or counts (not only narrative).
```

## Test queries
- "How many rows are in dbo.gold_conveyor_event_fact_flat?"
- "List distinct run_id values and their counts from dbo.gold_conveyor_event_fact_flat."
- "Using dbo.gold_latest_run, how many events are in that run? How many have label_prior=1?"
- "Show top 10 anomalies by vibration_max and motor_current_max (include event_id)."
- "For top 5 anomalies, return event_id and evidence_frames_json."
