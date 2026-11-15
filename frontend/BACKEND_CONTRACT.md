# Frontend integration contract

The frontend is deliberately limited to the interaction layer. It expects one job-oriented backend endpoint and does not decide how components are grouped or how trays are packed.

## Submit a job

`POST /api/organizer-jobs` uses `multipart/form-data`:

- `game_name` (required string)
- `reference_photos[]` (four images of components alongside a ruler)

The backend uses the game name to retrieve the relevant rulebook/component roster, then uses it as context for CV classification of the submitted ruler photos. If image scale or a component match is low confidence, it returns a measurement question; otherwise it returns a job to poll:

```json
{"status":"needs_measurement","question":{"object":"Standard playing card","dimension":"width_mm","suggested_value":63}}
```

```json
{"status":"processing","job_id":"job_123"}
```

## Answer a measurement question

`POST /api/organizer-jobs/job_123/measurements`

```json
{"object":"Standard playing card","width_mm":63}
```

## Completed job

`GET /api/organizer-jobs/job_123` returns the generator's `layout.json` shape plus downloadable STL URLs:

```json
{
  "status":"complete",
  "layout": {"units":"mm", "box_inner_mm":{"width":210,"length":210,"height":50}, "trays":[]},
  "files":[{"name":"Playing cards","stl_url":"/files/job_123/01_playing_cards.stl"}]
}
```

The browser renders the returned STL URLs and treats `layout.trays` as the display source of truth. The backend owns lookup, Claude/CV calls, grouping, packing, and STL generation. Keep all provider keys on that backend; the browser should never call an AI provider with a production credential.
