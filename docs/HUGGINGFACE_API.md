# Hugging Face API Reference

This document summarizes the public Hugging Face APIs used by Hearth's model
search and download workflows.

## Base URLs

- Web UI: `https://huggingface.co`
- API: `https://huggingface.co/api`
- Model Hub search: `https://huggingface.co/api/models`

## Authentication

Set `HF_TOKEN` (or `HUGGINGFACE_TOKEN`) for private models or higher rate limits.

```bash
export HF_TOKEN="hf_..."
```

## Common Endpoints

### Search models

`GET /api/models`

Query parameters:
- `search` – search text
- `filter` – pipeline tag (e.g. `text-generation`)
- `sort` – `downloads`, `likes`, `lastModified`, etc.
- `direction` – `-1` (descending) or `1`
- `limit` – page size
- `full` – include full metadata

Example:
```
GET https://huggingface.co/api/models?search=llama&sort=downloads&direction=-1&limit=20
```

### Get model metadata

`GET /api/models/{repo_id}`

Example:
```
GET https://huggingface.co/api/models/meta-llama/Llama-3.1-8B
```

### List files in a repository

`GET /api/models/{repo_id}/tree/{revision}`

Example:
```
GET https://huggingface.co/api/models/meta-llama/Llama-3.1-8B/tree/main
```

### Download a file

`GET https://huggingface.co/{repo_id}/resolve/{revision}/{path}`

Example:
```
GET https://huggingface.co/meta-llama/Llama-3.1-8B/resolve/main/config.json
```

## Python helpers (huggingface_hub)

```python
from huggingface_hub import HfApi, hf_hub_download

api = HfApi()
models = api.list_models(search="text-generation", sort="downloads", direction=-1, limit=10)

path = hf_hub_download(repo_id="meta-llama/Llama-3.1-8B", filename="config.json")
```

## Rate limits and errors

- Unauthenticated requests are rate-limited.
- Private repos return `401`/`403` without a valid token.
- Large downloads should use `hf_hub_download` (supports resume and caching).

## Playwright notes

The Playwright script in `tools/huggingface_playwright_download.py` automates the
web UI for discovery and downloads. For production use, prefer the API or
`huggingface_hub` helpers above.
