# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A single Python script ([download_templates.py](download_templates.py)) that renders image-fill nodes from a Figma file via the export-preview endpoint and writes one folder per wallpaper template, plus three local post-processors (`--fix-ext`, `--rotate`, `--resize`) driven by per-folder `data.json` manifests. Not a library, no tests, no build step.

## Setup & run

One-time setup:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Config lives in [.env](.env) (gitignored): `FIGMA_TOKEN` (personal access token, read scope is enough) and `FIGMA_FILE_KEY`. The script has a hand-rolled `.env` parser — do not add a `python-dotenv` dependency. External deps: `requests` (HTTP) and `Pillow` (post-process image ops).

### Download

```sh
# Everything: every Template_* frame on every page
.venv/bin/python download_templates.py

# Only one or more pages (exact match; quote names with spaces)
.venv/bin/python download_templates.py --page "Page 1"
.venv/bin/python download_templates.py --page "Page 1" --page "Page 2"

# Specific templates by id (the part after Template_)
.venv/bin/python download_templates.py --template 91 --template 26

# Combine page + template filters
.venv/bin/python download_templates.py --page "Page 1" --template 91
```

Unknown `--page` or `--template` values exit with status 1 so typos don't silently produce zero results.

### Post-process (operate on already-downloaded folders)

```sh
# Rename file extensions to match each folder's data.json srcName
.venv/bin/python download_templates.py --fix-ext
.venv/bin/python download_templates.py --fix-ext --template 71

# Un-rotate img* files using `angle` from data.json (idempotent via .rotated.json)
.venv/bin/python download_templates.py --rotate
.venv/bin/python download_templates.py --rotate --template 49
.venv/bin/python download_templates.py --rotate --rotate-prefix img   # override prefix

# Resize img* files to width × height from data.json (aspect-preserving)
.venv/bin/python download_templates.py --resize
.venv/bin/python download_templates.py --resize --template 102
```

Recommended ordering when chaining: `--fix-ext` → `--rotate` → `--resize`. Post-processors don't call the Figma API, so they never trigger rate limits.

## Architecture

The script hits two Figma REST endpoints:

1. `GET /v1/files/{key}` — full document tree. Walked recursively by `iter_template_frames`, which yields any `FRAME` node whose `name` starts with `Template_` (the prefix is the convention used by this specific Figma file; it is NOT a Figma feature). Wrapped in `get_with_retry`, which honors `Retry-After` on HTTP 429.
2. `GET /v1/images/{key}?ids=...&format=...&scale=...` — Figma's render endpoint, the same one powering export preview. Returns a `node_id -> presigned S3 URL` map. URLs are short-lived; do not cache them.

For each template frame, `iter_image_nodes` recursively yields every node that has at least one `IMAGE` fill. `render_params(node)` derives `(format, scale)` from the first entry of the node's `exportSettings`, falling back to PNG @ 1x for nodes that don't have any. `WIDTH`/`HEIGHT` constraints collapse to 1x because the `/v1/images` endpoint only accepts a numeric scale.

### Batching, resume, rate limits

Frames are bucketed into `folder_entries: {folder_name: [(node_id, node_name, fmt, scale), ...]}` and processed in groups of `TEMPLATE_BATCH_SIZE` (default 10) with a `BATCH_PAUSE_SECONDS` (2s) gap between groups — this spreads `/v1/images` calls so a single run doesn't blow the per-minute API budget. Within a batch, nodes are grouped by `(format, scale)` and each group is chunked at `RENDER_CHUNK_SIZE` (100) for URL-length safety.

Before processing a batch, `folder_is_complete` checks each folder against the expected file count. Folders that already have ≥ that many files are skipped — and if **every** folder in a batch is skippable, the inter-batch pause is also skipped. This makes a re-run after a 429 storm or crash blast through completed work without any API calls.

### Output

`output/<template-id>/<image-layer-name>.<ext>`, where `<template-id>` is the part of the frame name after `Template_` (e.g. frame `Template_91` → folder `91`).

Two consequences worth knowing:

- **Same `Template_NN` ID appears on multiple pages** (variants). Variants merge into one folder; each variant's image nodes have distinct node IDs so each variant renders separately. Name collisions inside the folder get `_1`, `_2` suffixes via `unique_path`. Use `--page` to keep only one variant.
- **Format is known a priori** from `render_params`, so the saved extension comes from `FORMAT_EXT[fmt]`, not the HTTP `Content-Type`. If the layer name already ends in a known image extension, that extension is stripped from the stem and the render-format extension is used.

### Post-processors

Each one walks `output/*/data.json` (a pre-existing manifest the downstream wallpaper renderer reads) and modifies files in place. Pillow is required for any operation that touches pixels.

- **`--fix-ext`** ([download_templates.py](download_templates.py) `post_process_extensions`). For each `srcName` in data.json, finds a file with the same stem and renames it to match `srcName` exactly. Only the filename changes; bytes are untouched. Also rewrites keys in `.rotated.json` so a later `--rotate` re-run keeps tracking the same file.
- **`--rotate`** (`post_process_rotations`). For elements whose `srcName` starts with `--rotate-prefix` (default `img`) and have a non-zero `angle`, rotates the file by `-angle` (so the file ends axis-aligned; the `angle` field in data.json stays for the runtime renderer). `unrotate_image` then crops the alpha bounding box with `crop_transparent_border` so rotation padding doesn't get baked into white when the result is a JPG. Idempotent via `output/<id>/.rotated.json`.
- **`--resize`** (`post_process_resize`). Resizes each prefixed file to the `width` × `height` declared in data.json, aspect-preserving (target height is derived from the target width × current aspect ratio). Also runs `crop_transparent_border` first so transparent padding doesn't survive a JPG flatten.

Saving an RGBA result to a `.jpg`/`.jpeg` path triggers a white-background flatten via `Image.new("RGB", ..., (255,255,255))` + alpha paste, because JPG has no alpha channel.

## Things to know before changing it

- **Resume is coarse: file-count compare.** `folder_is_complete` only counts files; it doesn't verify the *right* files are there. Partial folders (some images written before a crash) re-download the missing ones — new files get `_1`/`_2` suffixes from `unique_path` since the existing files block their preferred names. To force a clean re-render, delete the folder (or its image files) and re-run.
- **Frame selection is name-based.** Changing the prefix means editing `TEMPLATE_PREFIX`. If you want to select by page instead of by name, replace `iter_template_frames` — don't add a second filter on top.
- **`safe_name` strips filesystem-invalid characters** but keeps the rest of the name verbatim, so folder names match the Figma layer names exactly (this matters if a downstream app keys off them).
- **Image fills only** (download path). Vector-only templates, text, or frames rendered as a composite will produce empty folders.
- **Post-processors edit in place; no backup.** A bad run can't be undone locally — the original Figma pixels are only recoverable by re-downloading. Worth keeping in mind before iterating on `unrotate_image`, `post_process_resize`, etc.
- **Filename ↔ node matching is fragile under collisions.** Post-processors find files by `srcName` stem (`find_downloaded_file`). They don't follow `_1`/`_2` suffixes since disk can't tell which Figma node those came from. If two nodes share a layer name within a template, only the first matched file is processed.
- **JPG ↔ RGBA flattening uses white.** `unrotate_image` and `post_process_resize` both composite onto `(255,255,255)` when the destination is `.jpg`/`.jpeg`. Change the tuple in both places if you need a different background colour.
