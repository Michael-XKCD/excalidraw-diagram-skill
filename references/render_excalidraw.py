"""Render Excalidraw JSON to PNG using Playwright + headless Chromium.

Usage:
    cd ~/.agents/skills/excalidraw-diagram/references
    uv run python render_excalidraw.py <path-to-file.excalidraw> [--output path.png] [--scale 2] [--width 1920]

First-time setup:
    cd ~/.agents/skills/excalidraw-diagram/references
    uv sync
    uv run playwright install chromium
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


MAX_INPUT_BYTES = 5 * 1024 * 1024
MAX_ELEMENTS = 5000
MAX_LINEAR_POINTS = 2000
MAX_ABSOLUTE_COORDINATE = 250_000
MAX_ELEMENT_DIMENSION = 100_000
MAX_RENDERED_DIMENSION = 20_000
MODULE_READY_TIMEOUT_MS = 30_000
RENDER_TIMEOUT_MS = 15_000


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_excalidraw(data: dict) -> list[str]:
    """Validate Excalidraw JSON structure. Returns list of errors (empty = valid)."""
    errors: list[str] = []

    if data.get("type") != "excalidraw":
        errors.append(f"Expected type 'excalidraw', got '{data.get('type')}'")

    if "elements" not in data:
        errors.append("Missing 'elements' array")
    elif not isinstance(data["elements"], list):
        errors.append("'elements' must be an array")
    elif len(data["elements"]) == 0:
        errors.append("'elements' array is empty — nothing to render")
    elif len(data["elements"]) > MAX_ELEMENTS:
        errors.append(
            f"'elements' array exceeds safe limit ({len(data['elements'])} > {MAX_ELEMENTS})"
        )

    files = data.get("files", {})
    if files is not None and not isinstance(files, dict):
        errors.append("'files' must be an object when present")

    app_state = data.get("appState", {})
    if app_state is not None and not isinstance(app_state, dict):
        errors.append("'appState' must be an object when present")

    for index, element in enumerate(data.get("elements", [])):
        if not isinstance(element, dict):
            errors.append(f"Element {index} must be an object")
            continue

        element_id = element.get("id", f"index {index}")
        element_type = element.get("type")
        if not isinstance(element_type, str) or not element_type:
            errors.append(f"Element {element_id} is missing a valid 'type'")

        for field in ("x", "y", "width", "height"):
            value = element.get(field, 0)
            if not _is_number(value):
                errors.append(f"Element {element_id} has non-numeric '{field}'")
                continue
            if abs(float(value)) > MAX_ABSOLUTE_COORDINATE:
                errors.append(f"Element {element_id} has '{field}' outside safe bounds")
            if (
                field in {"width", "height"}
                and abs(float(value)) > MAX_ELEMENT_DIMENSION
            ):
                errors.append(
                    f"Element {element_id} has '{field}' larger than safe limit"
                )

        if element_type in {"arrow", "line"}:
            points = element.get("points")
            if not isinstance(points, list) or len(points) < 2:
                errors.append(f"Element {element_id} must define at least 2 points")
                continue
            if len(points) > MAX_LINEAR_POINTS:
                errors.append(
                    f"Element {element_id} exceeds safe point count ({len(points)} > {MAX_LINEAR_POINTS})"
                )
                continue
            for point_index, point in enumerate(points):
                if not isinstance(point, list) or len(point) != 2:
                    errors.append(
                        f"Element {element_id} point {point_index} must be a 2-item array"
                    )
                    continue
                if not all(_is_number(coord) for coord in point):
                    errors.append(
                        f"Element {element_id} point {point_index} must contain numbers"
                    )
                    continue
                if max(abs(float(coord)) for coord in point) > MAX_ABSOLUTE_COORDINATE:
                    errors.append(
                        f"Element {element_id} point {point_index} is outside safe bounds"
                    )

    return errors


def compute_bounding_box(elements: list[dict]) -> tuple[float, float, float, float]:
    """Compute bounding box (min_x, min_y, max_x, max_y) across all elements."""
    min_x = float("inf")
    min_y = float("inf")
    max_x = float("-inf")
    max_y = float("-inf")

    for el in elements:
        if el.get("isDeleted"):
            continue
        x = el.get("x", 0)
        y = el.get("y", 0)
        w = el.get("width", 0)
        h = el.get("height", 0)

        # For arrows/lines, points array defines the shape relative to x,y
        if el.get("type") in ("arrow", "line") and "points" in el:
            for px, py in el["points"]:
                min_x = min(min_x, x + px)
                min_y = min(min_y, y + py)
                max_x = max(max_x, x + px)
                max_y = max(max_y, y + py)
        else:
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x + abs(w))
            max_y = max(max_y, y + abs(h))

    if min_x == float("inf"):
        return (0, 0, 800, 600)

    return (min_x, min_y, max_x, max_y)


def render(
    excalidraw_path: Path,
    output_path: Path | None = None,
    scale: int = 2,
    max_width: int = 1920,
) -> Path:
    """Render an .excalidraw file to PNG. Returns the output PNG path."""
    # Import playwright here so validation errors show before import errors
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright not installed.", file=sys.stderr)
        print(
            "Run: cd ~/.agents/skills/excalidraw-diagram/references && uv sync && uv run playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)

    # Read and validate
    raw_bytes = excalidraw_path.read_bytes()
    if len(raw_bytes) > MAX_INPUT_BYTES:
        print(
            f"ERROR: Input file is too large ({len(raw_bytes)} bytes > {MAX_INPUT_BYTES} bytes safe limit).",
            file=sys.stderr,
        )
        sys.exit(1)

    raw = raw_bytes.decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {excalidraw_path}: {e}", file=sys.stderr)
        sys.exit(1)

    errors = validate_excalidraw(data)
    if errors:
        print(f"ERROR: Invalid Excalidraw file:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    # Compute viewport size from element bounding box
    elements = [e for e in data["elements"] if not e.get("isDeleted")]
    min_x, min_y, max_x, max_y = compute_bounding_box(elements)
    padding = 80
    diagram_w = max_x - min_x + padding * 2
    diagram_h = max_y - min_y + padding * 2
    if diagram_w > MAX_RENDERED_DIMENSION or diagram_h > MAX_RENDERED_DIMENSION:
        print(
            "ERROR: Diagram exceeds safe render bounds "
            f"({int(diagram_w)}x{int(diagram_h)} > {MAX_RENDERED_DIMENSION}px limit).",
            file=sys.stderr,
        )
        sys.exit(1)

    # Cap viewport width, let height be natural
    vp_width = min(int(diagram_w), max_width)
    vp_height = max(int(diagram_h), 600)

    # Output path
    if output_path is None:
        output_path = excalidraw_path.with_suffix(".png")

    # Template path (same directory as this script)
    template_path = Path(__file__).parent / "render_template.html"
    if not template_path.exists():
        print(f"ERROR: Template not found at {template_path}", file=sys.stderr)
        sys.exit(1)

    template_url = template_path.as_uri()

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:
            if "Executable doesn't exist" in str(e) or "browserType.launch" in str(e):
                print("ERROR: Chromium not installed for Playwright.", file=sys.stderr)
                print(
                    "Run: cd ~/.agents/skills/excalidraw-diagram/references && uv run playwright install chromium",
                    file=sys.stderr,
                )
                sys.exit(1)
            raise

        page = browser.new_page(
            viewport={"width": vp_width, "height": vp_height},
            device_scale_factor=scale,
        )
        page.set_default_timeout(RENDER_TIMEOUT_MS)

        # Load the template
        page.goto(template_url)

        # Wait for the ES module to load.
        page.wait_for_function(
            "window.__moduleReady === true", timeout=MODULE_READY_TIMEOUT_MS
        )

        # Inject the diagram data and render
        result = page.evaluate("data => window.renderDiagram(data)", data)

        if not result or not result.get("success"):
            error_msg = (
                result.get("error", "Unknown render error")
                if result
                else "renderDiagram returned null"
            )
            print(f"ERROR: Render failed: {error_msg}", file=sys.stderr)
            browser.close()
            sys.exit(1)

        # Wait for render completion signal
        page.wait_for_function(
            "window.__renderComplete === true", timeout=RENDER_TIMEOUT_MS
        )

        # Screenshot the SVG element
        svg_el = page.query_selector("#root svg")
        if svg_el is None:
            print("ERROR: No SVG element found after render.", file=sys.stderr)
            browser.close()
            sys.exit(1)

        # Resize viewport to fit the actual rendered SVG dimensions after
        # viewBox expansion in the template. This prevents screenshot clipping.
        svg_box = svg_el.bounding_box()
        if svg_box:
            fit_w = max(1, int(svg_box["width"]))
            fit_h = max(1, int(svg_box["height"]))
            if fit_w > MAX_RENDERED_DIMENSION or fit_h > MAX_RENDERED_DIMENSION:
                print(
                    "ERROR: Rendered SVG exceeds safe screenshot bounds "
                    f"({fit_w}x{fit_h} > {MAX_RENDERED_DIMENSION}px limit).",
                    file=sys.stderr,
                )
                browser.close()
                sys.exit(1)
            page.set_viewport_size({"width": fit_w, "height": fit_h})

        svg_el.screenshot(path=str(output_path))
        browser.close()

    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Render Excalidraw JSON to PNG")
    parser.add_argument("input", type=Path, help="Path to .excalidraw JSON file")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output PNG path (default: same name with .png)",
    )
    parser.add_argument(
        "--scale", "-s", type=int, default=2, help="Device scale factor (default: 2)"
    )
    parser.add_argument(
        "--width",
        "-w",
        type=int,
        default=1920,
        help="Max viewport width (default: 1920)",
    )
    args = parser.parse_args()

    if not args.input.exists():
        print(f"ERROR: File not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    png_path = render(args.input, args.output, args.scale, args.width)
    print(str(png_path))


if __name__ == "__main__":
    main()
