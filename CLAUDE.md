# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A single Python script ([download_templates.py](download_templates.py)) that renders image-fill nodes from a Figma file via the export-preview endpoint and writes one folder per wallpaper template. Not a library, no tests, no build step.

## Setup & run

One-time setup:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Config lives in [.env](.env) (gitignored): `FIGMA_TOKEN` (personal access token, read scope is enough) and `FIGMA_FILE_KEY`. The script has a hand-rolled `.env` parser — do not add a `python-dotenv` dependency. Only external dep is `requests`.

Run modes:

```sh
# Everything: every Template_* frame on every page
.venv/bin/python download_templates.py

# Only one page (exact name match; quote names with spaces)
.venv/bin/python download_templates.py --page "Page 1"

# Multiple pages
.venv/bin/python download_templates.py --page "Page 1" --page "Page 2"

# Specific templates by id (id is the part after Template_)
.venv/bin/python download_templates.py --template 91 --template 26

# Combine: only those template ids, only on that page
.venv/bin/python download_templates.py --page "Page 1" --template 91
```

Unknown `--page` or `--template` values exit with status 1 so typos don't silently produce zero results.

## Architecture

The script hits two Figma REST endpoints:

1. `GET /v1/files/{key}` — full document tree. Walked recursively by `iter_template_frames`, which yields any `FRAME` node whose `name` starts with `Template_` (the prefix is the convention used by this specific Figma file; it is NOT a Figma feature).
2. `GET /v1/images/{key}?ids=...&format=...&scale=...` — Figma's render endpoint, the same one powering export preview. Returns a `node_id -> presigned S3 URL` map. URLs are short-lived; do not cache them.

For each template frame, `iter_image_nodes` recursively yields every node that has at least one `IMAGE` fill (we render the same selection that the original-fill version downloaded, but as Figma would export it). `render_params(node)` derives `(format, scale)` from the first entry of the node's `exportSettings`, falling back to PNG @ 1x for nodes that don't have any. `WIDTH`/`HEIGHT` constraints collapse to 1x because the `/v1/images` endpoint only accepts a numeric scale.

Nodes are collected into `node_meta: {node_id: (folder_name, node_name)}` and grouped by `(format, scale)` into `render_groups`. Each group is fetched in chunks of `RENDER_CHUNK_SIZE` (URL-length safety) and the resulting URLs are routed back to their folders.

Two consequences:

- **Same `Template_NN` ID appears on multiple pages** (variants). All variants merge into one folder, but each variant's image nodes have distinct node ids, so each variant renders separately — name collisions inside the folder get `_1`, `_2` suffixes via `unique_path`. The script no longer collapses "same underlying asset across variants" into one file the way the old `imageRef` dedupe did; use `--page` if you only want one variant.
- **Format is known a priori** from `render_params`, so the saved extension comes from `FORMAT_EXT[fmt]`, not the HTTP response. If the layer name already ends in a known image extension, the stem is reused and the export-format extension is appended.

Output layout: `output/<template-id>/<image-layer-name>.<ext>`, where `<template-id>` is the part of the frame name after `Template_` (e.g. frame `Template_91` → folder `91`).

## Things to know before changing it

- **No skip-if-exists.** Re-running downloads everything again and `unique_path` will create `name_1.png`, `name_2.png`, etc. If you need idempotent re-runs, add an existence check before `download_image`.
- **Frame selection is name-based.** Changing the prefix means editing `TEMPLATE_PREFIX`. If you want to select by page instead of by name, replace `iter_template_frames` — don't add a second filter on top.
- **`safe_name` strips filesystem-invalid characters** but keeps the rest of the name verbatim, so folder names match the Figma layer names exactly (this matters if a downstream app keys off them).
- **Image fills only.** Vector-only templates, text, or frames rendered as a composite will produce empty folders (the script removes nothing — empty folders just sit there).
