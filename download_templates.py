"""Download rendered image-layer exports from every Template_* frame in a Figma file.

For each frame whose name starts with "Template_", create output/<frame-name>/ and
write every image-fill node inside that frame as Figma would render it via export
preview (honoring each node's exportSettings, defaulting to PNG @ 1x).
"""

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
TEMPLATE_PREFIX = "Template_"
INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

RENDER_FORMATS = {"png", "jpg", "svg", "pdf"}
FORMAT_EXT = {"png": ".png", "jpg": ".jpg", "svg": ".svg", "pdf": ".pdf"}

KNOWN_IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".svg", ".bmp", ".tif", ".tiff", ".heic", ".heif",
    ".pdf",
}

# Figma's /v1/images caps the URL length; chunk node ids to stay well under it.
RENDER_CHUNK_SIZE = 100

# Process templates in groups so a single run doesn't blow through the
# per-minute API budget. After each group we pause before starting the next.
TEMPLATE_BATCH_SIZE = 10
BATCH_PAUSE_SECONDS = 2.0

# How many times to retry a request that comes back HTTP 429. Figma's
# `Retry-After` header is honored when set; otherwise we wait a default.
RATE_LIMIT_RETRIES = 5
RATE_LIMIT_DEFAULT_WAIT = 30


def load_env():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def safe_name(name: str) -> str:
    cleaned = INVALID_FS_CHARS.sub("_", name).strip().strip(".")
    return cleaned or "unnamed"


def normalize_match_key(name: str) -> str:
    """Normalize a name for matching Figma node names against data.json srcNames.

    Both sides may or may not carry an image extension (e.g. Figma layer "bg"
    vs. data.json "bg.jpg"), so we strip any known image extension before
    comparing. The result is also filesystem-safe so case-mismatched or
    decorated names still match.
    """
    safe = safe_name(name)
    p = Path(safe)
    if p.suffix.lower() in KNOWN_IMAGE_EXTS:
        return p.stem.lower()
    return safe.lower()


def load_data_json_targets(folder: Path) -> dict[str, str]:
    """Return {normalize_match_key(srcName): srcName} for `folder/data.json`.

    Empty dict if data.json is missing or unreadable. The returned `srcName`
    is the exact filename (with extension) we should write to disk so the
    downstream renderer finds it.
    """
    path = folder / "data.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    targets: dict[str, str] = {}
    for element in data.get("elements") or []:
        src = element.get("srcName")
        if isinstance(src, str) and src:
            targets[normalize_match_key(src)] = src
    return targets


def iter_template_frames(node):
    if node.get("type") == "FRAME" and node.get("name", "").startswith(TEMPLATE_PREFIX):
        yield node
        return
    for child in node.get("children", []) or []:
        yield from iter_template_frames(child)


def iter_image_nodes(node):
    """Yield every node that has at least one IMAGE fill."""
    for fill in node.get("fills", []) or []:
        if fill.get("type") == "IMAGE":
            yield node
            break
    for child in node.get("children", []) or []:
        yield from iter_image_nodes(child)


def node_rotation_degrees(node) -> float:
    """Return the node's own rotation in degrees (0 if axis-aligned).

    Prefers the explicit `rotation` field when Figma provides it; falls back
    to deriving the angle from `relativeTransform[0][0]` / `relativeTransform[1][0]`
    so rotated nodes that omit the field still get detected.
    """
    rotation = node.get("rotation")
    if rotation is not None:
        return float(rotation)
    rt = node.get("relativeTransform")
    if rt and len(rt) >= 2 and len(rt[0]) >= 3:
        return math.degrees(math.atan2(rt[1][0], rt[0][0]))
    return 0.0


# PIL can rewrite these in place; svg/pdf cannot, so we leave them rotated and warn.
UNROTATABLE_FORMATS = {"svg", "pdf"}


def crop_transparent_border(img: "Image.Image") -> "Image.Image":
    """Crop fully-transparent padding off an RGBA/LA image.

    Uses the alpha channel's bounding box (not the RGB bbox) so that
    transparent-but-non-black padding — e.g. the triangles a rotation leaves
    behind — is removed. Returns the image unchanged when there's no alpha
    or nothing to trim.
    """
    if img.mode not in ("RGBA", "LA"):
        return img
    bbox = img.getchannel("A").getbbox()
    if bbox and bbox != (0, 0, img.width, img.height):
        return img.crop(bbox)
    return img


def unrotate_image(path: Path, angle_degrees: float) -> None:
    """Rotate the file at `path` by -angle so the saved image is axis-aligned.

    Figma's render endpoint always bakes node rotation into the export. To
    match a Figma user's mental model of "the image without rotation", we
    invert that rotation in place, then crop off the transparent triangles
    the rotation leaves around the content — otherwise that padding would
    later be flattened into visible white when the file is saved as JPG.
    PNG outputs keep transparency; JPG (no alpha) is flattened onto white.

    Side effect: this also re-encodes the file as whatever the target
    extension implies, so a `.jpg` file with PNG bytes (e.g. left over from
    `--fix-ext`) becomes a real JPG on disk after this runs.
    """
    if abs(angle_degrees) < 0.01:
        return
    with Image.open(path) as img:
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        rotated = img.rotate(-angle_degrees, expand=True, resample=Image.BICUBIC)
        rotated = crop_transparent_border(rotated)
        if path.suffix.lower() in (".jpg", ".jpeg"):
            background = Image.new("RGB", rotated.size, (255, 255, 255))
            background.paste(rotated, mask=rotated.split()[3])
            background.save(path)
        else:
            rotated.save(path)


def render_params(node) -> tuple[str, float]:
    """Return (format, scale) matching Figma's export preview for this node.

    Honors the first entry of `exportSettings` when present; falls back to
    PNG @ 1x — the same default Figma's export panel shows for an
    unconfigured node. WIDTH/HEIGHT constraints aren't expressible on the
    images endpoint, so we collapse those to 1x.
    """
    settings_list = node.get("exportSettings") or []
    if not settings_list:
        return "png", 1
    s = settings_list[0]
    fmt = (s.get("format") or "PNG").lower()
    if fmt not in RENDER_FORMATS:
        fmt = "png"
    constraint = s.get("constraint") or {}
    scale = 1
    if constraint.get("type") == "SCALE":
        scale = constraint.get("value") or 1
    return fmt, scale


def folder_is_complete(folder: Path, expected_count: int) -> bool:
    """True if `folder` already contains at least `expected_count` files.

    Used to skip templates that finished in a previous run. The check is
    coarse — it doesn't verify the *right* files are there — but it lets a
    crashed/rate-limited run resume from where it left off without
    re-rendering everything that's already on disk.
    """
    if expected_count <= 0 or not folder.exists():
        return False
    count = sum(1 for p in folder.iterdir() if p.is_file())
    return count >= expected_count


def unique_path(folder: Path, base: str, ext: str) -> Path:
    candidate = folder / f"{base}{ext}"
    counter = 1
    while candidate.exists():
        candidate = folder / f"{base}_{counter}{ext}"
        counter += 1
    return candidate


def download_image(url: str, folder: Path, base_name: str, ext: str) -> Path:
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    name = Path(base_name)
    stem = name.stem if name.suffix.lower() in KNOWN_IMAGE_EXTS else base_name
    dest = unique_path(folder, stem, ext)
    with dest.open("wb") as fh:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if chunk:
                fh.write(chunk)
    return dest


def chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def get_with_retry(url, headers, params=None, timeout=120):
    """GET that retries on HTTP 429, honoring Retry-After when present."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        if resp.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
            return resp
        wait = int(resp.headers.get("Retry-After") or RATE_LIMIT_DEFAULT_WAIT)
        print(f"  rate limited (HTTP 429), sleeping {wait}s ({attempt + 1}/{RATE_LIMIT_RETRIES})")
        time.sleep(wait)
    return resp


def fetch_render_urls(api, file_key, headers, node_ids, fmt, scale):
    """Call /v1/images in chunks and return a flat {node_id: url} map."""
    urls: dict[str, str | None] = {}
    for chunk in chunked(node_ids, RENDER_CHUNK_SIZE):
        resp = get_with_retry(
            f"{api}/images/{file_key}",
            headers,
            params={"ids": ",".join(chunk), "format": fmt, "scale": scale},
            timeout=120,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("err"):
            raise RuntimeError(f"Figma render error ({fmt}@{scale}x): {payload['err']}")
        urls.update(payload.get("images") or {})
    return urls


ROTATED_MANIFEST = ".rotated.json"

# Extensions we'll consider when searching for a downloaded file matching a node.
LOOKUP_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def find_downloaded_file(folder: Path, base: str) -> Path | None:
    """Find a downloaded file in `folder` whose stem equals `base`.

    Tries each extension in `LOOKUP_EXTS`. Doesn't follow `_1`/`_2` collision
    suffixes — those are ambiguous (multiple Figma nodes with the same name
    can't be told apart from disk alone), so we leave them to the user.
    """
    for ext in LOOKUP_EXTS:
        candidate = folder / f"{base}{ext}"
        if candidate.is_file():
            return candidate
    return None


def load_rotated_manifest(folder: Path) -> dict[str, float]:
    path = folder / ROTATED_MANIFEST
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_rotated_manifest(folder: Path, manifest: dict[str, float]) -> None:
    (folder / ROTATED_MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True))


def post_process_resize(args) -> int:
    """Resize prefixed files to the `width` declared in data.json, keeping aspect.

    For each subfolder of `output/`, reads `data.json` and for every element
    whose `srcName` starts with `args.rotate_prefix`, scales the matching
    file so its width equals that element's `width`. The height follows the
    file's own aspect ratio (target_height = current_height * width / current_width)
    — the `height` field in data.json is not used. PIL re-encodes on save,
    honoring the file's current extension (white-flattens RGBA for JPG).
    """
    if not OUTPUT_DIR.exists():
        print(f"Output dir {OUTPUT_DIR} not found.", file=sys.stderr)
        return 1

    wanted_ids = set(args.template) if args.template else None
    prefix = args.rotate_prefix or ""

    resized = 0
    already_ok = 0
    not_found = 0
    bad_dims = 0
    folders_visited = 0
    folders_missing_data = 0

    for folder in sorted(p for p in OUTPUT_DIR.iterdir() if p.is_dir()):
        if wanted_ids is not None and folder.name not in wanted_ids:
            continue
        data_path = folder / "data.json"
        if not data_path.exists():
            folders_missing_data += 1
            continue
        try:
            data = json.loads(data_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  ?? {folder.name}/data.json unreadable: {exc}")
            continue

        folders_visited += 1

        for element in data.get("elements") or []:
            src = element.get("srcName") or ""
            if not src.startswith(prefix):
                continue
            width = element.get("width")
            if not isinstance(width, (int, float)) or int(width) <= 0:
                bad_dims += 1
                continue
            tw = int(width)

            target = folder / src
            if not target.is_file():
                target = find_downloaded_file(folder, Path(src).stem)
            if target is None or not target.is_file():
                not_found += 1
                print(f"  ?? {folder.name}/{src}: file not found")
                continue

            try:
                with Image.open(target) as img:
                    img = crop_transparent_border(img)
                    orig_w, orig_h = img.size
                    if orig_w == tw:
                        already_ok += 1
                        continue
                    th = max(1, round(orig_h * tw / orig_w))
                    out = img.resize((tw, th), Image.LANCZOS)
                    if target.suffix.lower() in (".jpg", ".jpeg") and out.mode == "RGBA":
                        background = Image.new("RGB", out.size, (255, 255, 255))
                        background.paste(out, mask=out.split()[3])
                        background.save(target)
                    else:
                        out.save(target)
                resized += 1
                print(
                    f"  ok {folder.name}/{target.name} "
                    f"({orig_w}x{orig_h} -> {tw}x{th})"
                )
            except (OSError, ValueError) as exc:
                print(f"  fail {folder.name}/{target.name}: {exc}")

    print(
        f"\nDone. Visited {folders_visited} folder(s) "
        f"({folders_missing_data} had no data.json). "
        f"{resized} resized, {already_ok} already correct width, "
        f"{not_found} not found, {bad_dims} missing/invalid width."
    )
    return 0


def post_process_extensions(args) -> int:
    """Rename files in output/<id>/ to match `srcName` in each folder's data.json.

    For each `srcName` listed in `output/<id>/data.json`, find a file in the
    folder with the same stem and rename it to `srcName` (extension included).
    Only the filename changes — file contents stay as Figma rendered them.

    Also rewrites keys in `.rotated.json` so an existing rotation manifest
    keeps tracking the same file after the rename.
    """
    if not OUTPUT_DIR.exists():
        print(f"Output dir {OUTPUT_DIR} not found.", file=sys.stderr)
        return 1

    wanted_ids = set(args.template) if args.template else None
    renamed = 0
    already_ok = 0
    not_found = 0
    conflict = 0
    folders_visited = 0
    folders_missing_data = 0

    for folder in sorted(p for p in OUTPUT_DIR.iterdir() if p.is_dir()):
        if wanted_ids is not None and folder.name not in wanted_ids:
            continue
        data_path = folder / "data.json"
        if not data_path.exists():
            folders_missing_data += 1
            continue
        try:
            data = json.loads(data_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  ?? {folder.name}/data.json unreadable: {exc}")
            continue

        folders_visited += 1
        manifest = load_rotated_manifest(folder)
        manifest_dirty = False

        for element in data.get("elements") or []:
            src = element.get("srcName")
            if not isinstance(src, str) or not src:
                continue
            target = folder / src
            if target.exists():
                already_ok += 1
                continue
            stem = Path(src).stem
            current = next(
                (p for p in folder.iterdir() if p.is_file() and p.stem == stem and p.name != "data.json"),
                None,
            )
            if current is None:
                not_found += 1
                print(f"  ?? {folder.name}/{src}: no source file with stem '{stem}'")
                continue
            if target.exists():
                conflict += 1
                print(f"  !! {folder.name}/{current.name} -> {src}: target exists, skipping")
                continue
            try:
                current.rename(target)
            except OSError as exc:
                print(f"  fail {folder.name}/{current.name} -> {src}: {exc}")
                continue
            if current.name in manifest:
                manifest[src] = manifest.pop(current.name)
                manifest_dirty = True
            renamed += 1
            print(f"  ok {folder.name}/{current.name} -> {src}")

        if manifest_dirty:
            save_rotated_manifest(folder, manifest)

    print(
        f"\nDone. Visited {folders_visited} folder(s) "
        f"({folders_missing_data} had no data.json). "
        f"{renamed} renamed, {already_ok} already correct, "
        f"{not_found} not found, {conflict} conflicts."
    )
    return 0


def post_process_rotations(args) -> int:
    """Walk output/*/data.json and un-rotate matching files in place.

    For each subfolder of `output/`, reads `data.json` and for every element
    whose `srcName` starts with `args.rotate_prefix` and has a non-zero
    `angle`, rotates the corresponding file by -angle (so the file ends up
    axis-aligned; the `angle` field in `data.json` is left untouched for the
    downstream runtime renderer to apply).

    Processed filenames are recorded in `output/<id>/.rotated.json` so that
    repeated invocations don't re-rotate the same files.
    """
    if not OUTPUT_DIR.exists():
        print(f"Output dir {OUTPUT_DIR} not found.", file=sys.stderr)
        return 1

    wanted_ids = set(args.template) if args.template else None
    prefix = args.rotate_prefix or ""

    rotated = 0
    already_done = 0
    not_found = 0
    skipped_zero = 0
    skipped_format = 0
    folders_visited = 0
    folders_missing_data = 0

    for folder in sorted(p for p in OUTPUT_DIR.iterdir() if p.is_dir()):
        if wanted_ids is not None and folder.name not in wanted_ids:
            continue

        data_path = folder / "data.json"
        if not data_path.exists():
            folders_missing_data += 1
            continue

        try:
            data = json.loads(data_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  ?? {folder.name}/data.json unreadable: {exc}")
            continue

        elements = data.get("elements") or []
        folders_visited += 1
        manifest = load_rotated_manifest(folder)
        dirty = False

        for element in elements:
            src = element.get("srcName") or ""
            if not src.startswith(prefix):
                continue
            angle = element.get("angle")
            if not angle:
                skipped_zero += 1
                continue

            base = Path(src).stem
            target = find_downloaded_file(folder, base)
            if target is None:
                not_found += 1
                print(f"  ?? {folder.name}/{src}: file not found")
                continue
            if target.suffix.lower().lstrip(".") in UNROTATABLE_FORMATS:
                skipped_format += 1
                continue
            if target.name in manifest:
                already_done += 1
                continue

            try:
                unrotate_image(target, float(angle))
                manifest[target.name] = float(angle)
                dirty = True
                rotated += 1
                print(f"  ok {folder.name}/{target.name} (un-rotated {angle}°)")
            except (OSError, ValueError) as exc:
                print(f"  fail {folder.name}/{target.name}: {exc}")

        if dirty:
            save_rotated_manifest(folder, manifest)

    print(
        f"\nDone. Visited {folders_visited} folder(s) "
        f"({folders_missing_data} had no data.json). "
        f"{rotated} rotated, {already_done} already rotated, "
        f"{not_found} not found, {skipped_zero} zero-angle, "
        f"{skipped_format} skipped (svg/pdf)."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--page",
        action="append",
        metavar="NAME",
        help="Only download templates from the named page. Repeatable. Exact match.",
    )
    parser.add_argument(
        "--template",
        action="append",
        metavar="ID",
        help="Only download these template ids (e.g. --template 91 for Template_91). Repeatable.",
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help=(
            "Post-process only: skip downloading. For each output/<id>/data.json, "
            "rotate every img*.png/jpg file by -angle so the saved image is "
            "axis-aligned (angle stays in data.json for the runtime renderer). "
            "Tracks done work in `.rotated.json` per folder; re-runs are idempotent."
        ),
    )
    parser.add_argument(
        "--rotate-prefix",
        default="img",
        metavar="PREFIX",
        help="In --rotate mode, only un-rotate elements whose srcName starts with this prefix (default: img).",
    )
    parser.add_argument(
        "--fix-ext",
        action="store_true",
        help=(
            "Post-process only: rename files in output/<id>/ so their "
            "extensions match the `srcName` listed in that folder's "
            "data.json. Does NOT re-encode bytes — only changes the extension."
        ),
    )
    parser.add_argument(
        "--resize",
        action="store_true",
        help=(
            "Post-process only: scale each file whose data.json element has "
            "srcName starting with --rotate-prefix (default `img`) so its "
            "width matches the `width` in data.json. Height follows the "
            "file's own aspect ratio. Re-encodes via PIL."
        ),
    )
    args = parser.parse_args()

    if args.fix_ext:
        return post_process_extensions(args)

    if args.resize:
        return post_process_resize(args)

    if args.rotate:
        return post_process_rotations(args)

    load_env()
    token = os.environ.get("FIGMA_TOKEN")
    file_key = os.environ.get("FIGMA_FILE_KEY")
    if not token or not file_key:
        print("FIGMA_TOKEN and FIGMA_FILE_KEY must be set in .env", file=sys.stderr)
        return 1

    headers = {"X-Figma-Token": token}
    api = "https://api.figma.com/v1"

    print(f"Fetching file tree {file_key}...")
    file_resp = get_with_retry(f"{api}/files/{file_key}", headers, timeout=120)
    file_resp.raise_for_status()
    document = file_resp.json()["document"]

    pages = document.get("children", [])
    if args.page:
        wanted = set(args.page)
        pages = [p for p in pages if p.get("name") in wanted]
        missing = wanted - {p.get("name") for p in pages}
        if missing:
            print(f"Page(s) not found: {sorted(missing)}", file=sys.stderr)
            return 1
        print(f"Filtering to pages: {[p['name'] for p in pages]}")

    wanted_ids = set(args.template) if args.template else None
    seen_ids: set[str] = set()

    # folder_name -> list of (node_id, node_name, fmt, scale). One folder per
    # Template_NN id (variants on different pages merge into the same folder).
    folder_entries: dict[str, list[tuple[str, str, str, float]]] = {}
    folders_in_order: list[str] = []
    seen_node_ids: set[str] = set()

    for page in pages:
        for frame in iter_template_frames(page):
            template_id = frame["name"][len(TEMPLATE_PREFIX):]
            if wanted_ids is not None and template_id not in wanted_ids:
                continue
            seen_ids.add(template_id)
            folder_name = safe_name(template_id)
            if folder_name not in folder_entries:
                folder_entries[folder_name] = []
                folders_in_order.append(folder_name)
            for node in iter_image_nodes(frame):
                node_id = node.get("id")
                if not node_id or node_id in seen_node_ids:
                    continue
                seen_node_ids.add(node_id)
                fmt, scale = render_params(node)
                folder_entries[folder_name].append(
                    (node_id, node.get("name") or node_id, fmt, scale)
                )

    if wanted_ids is not None:
        missing_ids = wanted_ids - seen_ids
        if missing_ids:
            print(f"Template id(s) not found: {sorted(missing_ids)}", file=sys.stderr)
            return 1

    if not folders_in_order:
        print("No Template_* frames with image fills found.")
        return 0

    OUTPUT_DIR.mkdir(exist_ok=True)
    total_images = 0
    total_failed = 0
    templates_complete = 0
    total_batches = (len(folders_in_order) + TEMPLATE_BATCH_SIZE - 1) // TEMPLATE_BATCH_SIZE
    prev_batch_hit_api = False

    for batch_idx, batch in enumerate(chunked(folders_in_order, TEMPLATE_BATCH_SIZE)):
        # Resume support: drop templates whose output folder already has at
        # least as many files as we'd download for them.
        pending: list[str] = []
        skipped_in_batch: list[str] = []
        for folder_name in batch:
            folder = OUTPUT_DIR / folder_name
            expected = len(folder_entries[folder_name])
            if folder_is_complete(folder, expected):
                skipped_in_batch.append(folder_name)
            else:
                pending.append(folder_name)
        templates_complete += len(skipped_in_batch)

        if not pending:
            print(
                f"\n=== batch {batch_idx + 1}/{total_batches}: "
                f"all {len(skipped_in_batch)} template(s) already complete, skipping ==="
            )
            prev_batch_hit_api = False
            continue

        if prev_batch_hit_api:
            print(f"\n--- pausing {BATCH_PAUSE_SECONDS}s before next batch ---")
            time.sleep(BATCH_PAUSE_SECONDS)
        prev_batch_hit_api = True

        print(
            f"\n=== batch {batch_idx + 1}/{total_batches}: "
            f"{len(pending)} pending, {len(skipped_in_batch)} already complete ==="
        )

        # Group this batch's pending nodes by (format, scale) — one render call per group.
        render_groups: dict[tuple[str, float], list[str]] = {}
        for folder_name in pending:
            for node_id, _, fmt, scale in folder_entries[folder_name]:
                render_groups.setdefault((fmt, scale), []).append(node_id)

        url_by_node: dict[str, str | None] = {}
        for (fmt, scale), node_ids in render_groups.items():
            print(f"  rendering {len(node_ids)} node(s) as {fmt}@{scale}x")
            urls = fetch_render_urls(api, file_key, headers, node_ids, fmt, scale)
            for nid in node_ids:
                url_by_node[nid] = urls.get(nid)

        for folder_name in pending:
            entries = folder_entries[folder_name]
            folder = OUTPUT_DIR / folder_name
            folder.mkdir(parents=True, exist_ok=True)
            print(f"\n  {folder_name} ({len(entries)} image(s))")
            for node_id, node_name, fmt, _ in entries:
                url = url_by_node.get(node_id)
                if not url:
                    print(f"    skip {node_name}: Figma returned no URL")
                    total_failed += 1
                    continue
                try:
                    saved = download_image(url, folder, safe_name(node_name), FORMAT_EXT[fmt])
                    print(f"    saved {saved.name}")
                    total_images += 1
                except requests.HTTPError as exc:
                    print(f"    fail {node_name}: {exc}")
                    total_failed += 1

    print(
        f"\nDone. {len(folders_in_order)} template folders "
        f"({templates_complete} already complete, {len(folders_in_order) - templates_complete} processed). "
        f"{total_images} images downloaded, {total_failed} failed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
