#!/usr/bin/env python3
"""Run live OpenCode baseline scenarios for the Excalidraw skill.

This harness is intentionally end-to-end:
1. Ask OpenCode to generate a diagram using the local skill materials.
2. Re-render the final .excalidraw artifact with the local renderer.
3. Ask OpenCode to review the rendered PNG and propose improvements.
4. Save all artifacts for later human ranking and taste calibration.

The first goal is not brittle pixel-perfect regression. The goal is to produce
reviewable baselines that can be ranked by a human and later reused as the
acceptance gate for renderer hardening work such as removing the esm.sh import.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).resolve()
TESTS_DIR = SCRIPT_PATH.parent
REPO_ROOT = TESTS_DIR.parent
SCENARIO_DIR = TESTS_DIR / "scenarios"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "artifacts" / "live-baseline"
DEFAULT_OCW_CONFIG = Path.home() / ".config" / "opencode-work" / "opencode.json"
DEFAULT_OCW_CONFIG_DIR = Path.home() / ".config" / "opencode-work"


@dataclass(frozen=True)
class Scenario:
    slug: str
    order: int
    title: str
    audience: str
    inspiration: list[str]
    review_focus: list[str]
    generation_prompt: str


class HarnessError(RuntimeError):
    """Raised when the live baseline harness cannot complete a scenario."""


def load_scenarios() -> list[Scenario]:
    scenarios: list[Scenario] = []
    for path in sorted(SCENARIO_DIR.glob("*.json")):
        payload = json.loads(path.read_text())
        scenarios.append(
            Scenario(
                slug=payload["slug"],
                order=int(payload.get("order", 999)),
                title=payload["title"],
                audience=payload["audience"],
                inspiration=list(payload.get("inspiration", [])),
                review_focus=list(payload.get("review_focus", [])),
                generation_prompt=payload["generation_prompt"],
            )
        )
    return sorted(scenarios, key=lambda item: (item.order, item.slug))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live OpenCode baseline scenarios")
    parser.add_argument(
        "--opencode-command",
        type=str,
        default="opencode",
        help="OpenCode executable to invoke (default: opencode)",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        default=[],
        help="Scenario slug to run. Repeat to run multiple scenarios.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Root directory for artifacts (default: {DEFAULT_OUTPUT_ROOT})",
    )
    parser.add_argument(
        "--run-label",
        type=str,
        default=None,
        help="Optional stable label for the run directory",
    )
    parser.add_argument(
        "--agent",
        type=str,
        default="auto",
        help="OpenCode agent name to use (default: auto)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Optional provider/model override for OpenCode",
    )
    parser.add_argument(
        "--generation-timeout",
        type=int,
        default=20 * 60,
        help="Generation timeout in seconds (default: 1200)",
    )
    parser.add_argument(
        "--review-timeout",
        type=int,
        default=10 * 60,
        help="Review timeout in seconds (default: 600)",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Continue running remaining scenarios after a failure",
    )
    parser.add_argument(
        "--max-fix-iterations",
        type=int,
        default=2,
        help="Maximum repair iterations after review finds blocker defects (default: 2)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available scenarios and exit",
    )
    return parser.parse_args()


def ensure_command_available(command: str) -> None:
    result = subprocess.run(
        [command, "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        env=build_opencode_env(),
    )
    if result.returncode != 0:
        raise HarnessError(f"Required command '{command}' is not available")


def sanitize_label(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "run"


def build_opencode_env() -> dict[str, str]:
    env = os.environ.copy()
    if "OPENCODE_CONFIG" not in env and DEFAULT_OCW_CONFIG.exists():
        env["OPENCODE_CONFIG"] = str(DEFAULT_OCW_CONFIG)
    if "OPENCODE_CONFIG_DIR" not in env and DEFAULT_OCW_CONFIG_DIR.exists():
        env["OPENCODE_CONFIG_DIR"] = str(DEFAULT_OCW_CONFIG_DIR)
    return env


def make_run_dir(output_root: Path, label: str | None) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = sanitize_label(label) if label else timestamp
    run_dir = output_root / suffix
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def format_generation_prompt(scenario: Scenario, artifact_dir: Path) -> str:
    diagram_path = artifact_dir / f"{scenario.slug}.excalidraw"
    prompt = scenario.generation_prompt.format(
        repo_root=REPO_ROOT,
        artifact_dir=artifact_dir,
        diagram_path=diagram_path,
    )
    precision_suffix = """

    This baseline cares heavily about execution precision, not just concept quality.
    Treat the following as blocker-level defects that must be fixed before you stop:
    - text flowing outside intended bounds or cramped enough to read as overflow
    - text colliding with nearby shapes, lines, or other text
    - arrows or connectors that do not visibly land on the right element
    - ambiguous labels or callouts that are not clearly attached to one target
    - sections that are so tight that rendering quality feels sloppy on inspection

    The human reviewer is specifically sensitive to two issues:
    - arrowheads or connector ends that appear even slightly detached from the intended target
    - text that appears to touch, cross, or visually press against a container or section boundary

    During your render-and-validate loop, focus especially on text bounds, annotation anchoring, and arrow landing positions. If needed, simplify content to preserve layout integrity.
    """
    return (
        textwrap.dedent(prompt).strip()
        + "\n\n"
        + textwrap.dedent(precision_suffix).strip()
    )


def format_review_prompt(scenario: Scenario, diagram_path: Path, png_path: Path) -> str:
    focus_lines = "\n".join(f"- {item}" for item in scenario.review_focus)
    return textwrap.dedent(
        f"""
        Review the attached rendered diagram and the source `.excalidraw` file.

        Context:
        - The diagram was generated using the local Excalidraw skill in `{REPO_ROOT / "SKILL.md"}`.
        - Audience: {scenario.audience}
        - Scenario: {scenario.title}
        - Focus areas:
        {focus_lines}

        Read `{REPO_ROOT / "SKILL.md"}` before reviewing so your critique is grounded in the skill's intended bar.
        Judge the attached PNG for visual quality, stakeholder legibility, and alignment with the skill rubric.
        Also propose changes that would improve the skill itself, not just this single diagram.

        Be strict about execution quality. The following count as blocker defects:
        - text that escapes intended bounds or feels visibly cramped against a container edge
        - text or annotations colliding with other content
        - arrows/connectors not landing cleanly on their intended targets
        - callouts or labels that are not clearly anchored to one element or arrow

        Use an unforgiving standard for two specific failure modes that the human reviewer cares about:
        - If an arrowhead or connector end does not visibly touch the target boundary or a clearly intentional anchor, treat it as a blocker.
        - If text touches, crosses, or visually presses against a box edge, section border, or nearby shape, treat it as a blocker.

        If any blocker defect is present, mark `ready_for_baseline` as false even if the overall concept is strong.

        Return JSON only. No markdown fences. Use this schema exactly:
        {{
          "scenario_slug": "{scenario.slug}",
          "overall_assessment": "strong|mixed|weak",
          "summary": "1-3 sentence assessment",
          "strengths": ["..."],
          "blocker_defects": [
            {{
              "type": "text-overflow|text-collision|arrow-attachment|label-anchoring|layout-cramping|other",
              "problem": "...",
              "proposed_change": "..."
            }}
          ],
          "issues": [
            {{
              "severity": "high|medium|low",
              "category": "composition|clarity|legibility|evidence|stakeholder-fit|skill-gap",
              "problem": "...",
              "proposed_change": "..."
            }}
          ],
          "taste_alignment": {{
            "fit": "strong|medium|weak",
            "notes": ["..."]
          }},
          "skill_improvements": ["..."],
          "ready_for_baseline": true
        }}
        """
    ).strip()


def format_fix_prompt(
    scenario: Scenario,
    diagram_path: Path,
    png_path: Path,
    review_payload: dict[str, Any],
) -> str:
    blocker_lines = "\n".join(
        f"- {item.get('type')}: {item.get('problem')} -> {item.get('proposed_change')}"
        for item in review_payload.get("blocker_defects", [])
    )
    if not blocker_lines:
        blocker_lines = "- No blocker defects listed."

    return textwrap.dedent(
        f"""
        Refine the existing Excalidraw diagram at `{diagram_path}`.

        Context:
        - Scenario: {scenario.title}
        - Audience: {scenario.audience}
        - Rendered PNG to inspect: `{png_path}`
        - The attached review JSON lists blocker defects that must be fixed before this diagram can be accepted as a baseline.

        Blocker defects to fix:
        {blocker_lines}

        Instructions:
        - Edit the existing `.excalidraw` file in place.
        - Prioritize layout integrity over adding more detail.
        - Eliminate text overflow, cramped captions, weak arrow landings, and floating labels.
        - If necessary, remove or simplify content to make the diagram cleaner.
        - Re-render and visually validate the diagram with the local renderer until those blocker defects are resolved.
        - Keep the core concept and audience fit intact.

        In your final response, briefly describe the fixes you made and confirm the saved path.
        """
    ).strip()


def run_subprocess(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if stdout_path is not None:
        stdout_path.write_text(result.stdout)
    if stderr_path is not None:
        stderr_path.write_text(result.stderr)
    return result


def parse_jsonl_events(raw_stream: str) -> tuple[str | None, list[dict[str, Any]]]:
    session_id: str | None = None
    events: list[dict[str, Any]] = []
    for raw_line in raw_stream.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HarnessError(
                f"Failed to parse OpenCode JSON event: {exc}: {line}"
            ) from exc
        events.append(event)
        session_id = session_id or event.get("sessionID")
    return session_id, events


def gather_text_from_events(events: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for event in events:
        if event.get("type") != "text":
            continue
        part = event.get("part", {})
        text = part.get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "\n".join(chunk.rstrip() for chunk in chunks if chunk.strip()).strip()


def extract_export_json(raw_text: str) -> dict[str, Any]:
    first_brace = raw_text.find("{")
    if first_brace == -1:
        raise HarnessError("OpenCode export output did not contain JSON")
    return json.loads(raw_text[first_brace:])


def export_session(
    session_id: str,
    artifact_dir: Path,
    prefix: str,
    opencode_command: str,
) -> dict[str, Any]:
    stdout_path = artifact_dir / f"{prefix}-session-export.txt"
    stderr_path = artifact_dir / f"{prefix}-session-export.stderr"
    result = run_subprocess(
        [opencode_command, "export", session_id],
        cwd=REPO_ROOT,
        timeout=120,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        env=build_opencode_env(),
    )
    if result.returncode != 0:
        raise HarnessError(f"OpenCode export failed for session {session_id}")
    try:
        payload = extract_export_json(result.stdout)
    except json.JSONDecodeError:
        payload = {
            "info": {"id": session_id},
            "export_parse_error": True,
            "raw_export_path": str(stdout_path),
        }
        (artifact_dir / f"{prefix}-session.parse-error.txt").write_text(result.stdout)
    (artifact_dir / f"{prefix}-session.json").write_text(json.dumps(payload, indent=2))
    return payload


def extract_first_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        tail = text[index + end :].strip()
        if isinstance(payload, dict) and not tail:
            return payload
        if isinstance(payload, dict) and tail.startswith("}"):
            return payload
        if isinstance(payload, dict) and tail == "":
            return payload
    raise HarnessError("Could not extract a JSON object from the review response")


def run_opencode_message(
    *,
    message: str,
    artifact_dir: Path,
    step_prefix: str,
    timeout: int,
    agent: str,
    model: str | None,
    opencode_command: str,
    attachments: list[Path] | None = None,
) -> tuple[str, dict[str, Any], str]:
    command = [
        opencode_command,
        "run",
        "--format",
        "json",
        "--dir",
        str(REPO_ROOT),
        "--agent",
        agent,
        "--title",
        step_prefix,
    ]
    if model:
        command.extend(["--model", model])
    for attachment in attachments or []:
        command.extend(["--file", str(attachment)])
    command.append("--")
    command.append(message)

    stdout_path = artifact_dir / f"{step_prefix}-events.jsonl"
    stderr_path = artifact_dir / f"{step_prefix}.stderr"
    result = run_subprocess(
        command,
        cwd=REPO_ROOT,
        timeout=timeout,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        env=build_opencode_env(),
    )
    if result.returncode != 0:
        raise HarnessError(
            f"OpenCode {step_prefix} failed with exit {result.returncode}. "
            f"See {stderr_path} and {stdout_path}."
        )

    session_id, events = parse_jsonl_events(result.stdout)
    if not session_id:
        raise HarnessError(f"OpenCode {step_prefix} did not emit a session ID")

    final_text = gather_text_from_events(events)
    (artifact_dir / f"{step_prefix}-response.txt").write_text(final_text + "\n")
    export_payload = export_session(
        session_id,
        artifact_dir,
        step_prefix,
        opencode_command,
    )
    return session_id, export_payload, final_text


def render_diagram(diagram_path: Path, png_path: Path, artifact_dir: Path) -> None:
    stdout_path = artifact_dir / "render.stdout"
    stderr_path = artifact_dir / "render.stderr"
    result = run_subprocess(
        [
            "uv",
            "run",
            "python",
            "render_excalidraw.py",
            str(diagram_path),
            "--output",
            str(png_path),
        ],
        cwd=REPO_ROOT / "references",
        timeout=300,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    if result.returncode != 0:
        raise HarnessError(f"Renderer failed for {diagram_path}; see {stderr_path}")
    if not png_path.exists():
        raise HarnessError(f"Expected rendered PNG was not created: {png_path}")


def write_taste_review_stub(
    scenario: Scenario, artifact_dir: Path, png_path: Path
) -> None:
    stub = textwrap.dedent(
        f"""
        # Taste Review Stub

        Scenario: {scenario.title}
        Slug: {scenario.slug}
        PNG: {png_path}

        Rank among the baseline set:
        - [ ] 1
        - [ ] 2
        - [ ] 3
        - [ ] 4
        - [ ] 5

        What I like:
        - 
        - 

        What I dislike:
        - 
        - 
        """
    ).strip()
    (artifact_dir / "taste-review.md").write_text(stub + "\n")


def record_generation_prompt(
    scenario: Scenario, artifact_dir: Path, prompt: str
) -> None:
    payload = {
        "slug": scenario.slug,
        "title": scenario.title,
        "audience": scenario.audience,
        "inspiration": scenario.inspiration,
        "review_focus": scenario.review_focus,
        "prompt": prompt,
    }
    (artifact_dir / "scenario.json").write_text(json.dumps(payload, indent=2))
    (artifact_dir / "prompt.txt").write_text(prompt + "\n")


def build_summary_row(
    *,
    scenario: Scenario,
    scenario_dir: Path,
    generation_session_id: str,
    review_session_id: str,
    review_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "slug": scenario.slug,
        "title": scenario.title,
        "artifact_dir": str(scenario_dir),
        "generation_session_id": generation_session_id,
        "review_session_id": review_session_id,
        "overall_assessment": review_payload.get("overall_assessment"),
        "blocker_defect_count": len(review_payload.get("blocker_defects", [])),
        "taste_alignment": review_payload.get("taste_alignment", {}).get("fit"),
        "ready_for_baseline": review_payload.get("ready_for_baseline"),
    }


def run_review(
    *,
    scenario: Scenario,
    scenario_dir: Path,
    diagram_path: Path,
    png_path: Path,
    timeout: int,
    agent: str,
    model: str | None,
    opencode_command: str,
    step_prefix: str,
) -> tuple[str, dict[str, Any], str]:
    review_prompt = format_review_prompt(scenario, diagram_path, png_path)
    (scenario_dir / f"{step_prefix}-prompt.txt").write_text(review_prompt + "\n")
    review_session_id, _review_export, review_text = run_opencode_message(
        message=review_prompt,
        artifact_dir=scenario_dir,
        step_prefix=step_prefix,
        timeout=timeout,
        agent=agent,
        model=model,
        opencode_command=opencode_command,
        attachments=[png_path, diagram_path],
    )
    review_payload = extract_first_json_object(review_text)
    (scenario_dir / f"{step_prefix}.json").write_text(
        json.dumps(review_payload, indent=2)
    )
    return review_session_id, review_payload, review_text


def run_fix_iteration(
    *,
    scenario: Scenario,
    scenario_dir: Path,
    diagram_path: Path,
    png_path: Path,
    review_payload: dict[str, Any],
    iteration: int,
    timeout: int,
    agent: str,
    model: str | None,
    opencode_command: str,
) -> str:
    step_prefix = f"fix-{iteration}"
    fix_prompt = format_fix_prompt(scenario, diagram_path, png_path, review_payload)
    (scenario_dir / f"{step_prefix}-prompt.txt").write_text(fix_prompt + "\n")
    fix_session_id, _fix_export, fix_text = run_opencode_message(
        message=fix_prompt,
        artifact_dir=scenario_dir,
        step_prefix=step_prefix,
        timeout=timeout,
        agent=agent,
        model=model,
        opencode_command=opencode_command,
        attachments=[
            png_path,
            diagram_path,
            scenario_dir / f"review-{iteration - 1}.json",
        ],
    )
    (scenario_dir / f"{step_prefix}-session-id.txt").write_text(fix_session_id + "\n")
    return fix_text


def write_run_summary(
    run_dir: Path, rows: list[dict[str, Any]], failures: list[dict[str, str]]
) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(REPO_ROOT),
        "results": rows,
        "failures": failures,
    }
    (run_dir / "summary.json").write_text(json.dumps(payload, indent=2))

    lines = [
        "# Live Baseline Summary",
        "",
        f"Run directory: `{run_dir}`",
        "",
        "## Results",
        "",
    ]
    if rows:
        for row in rows:
            lines.extend(
                [
                    f"- `{row['slug']}` - assessment: `{row['overall_assessment']}`, taste fit: `{row['taste_alignment']}`, ready: `{row['ready_for_baseline']}`",
                    f"  blocker defects: `{row['blocker_defect_count']}`",
                    f"  artifacts: `{row['artifact_dir']}`",
                ]
            )
    else:
        lines.append("- No successful scenarios.")

    lines.extend(["", "## Failures", ""])
    if failures:
        for failure in failures:
            lines.append(f"- `{failure['slug']}` - {failure['error']}")
    else:
        lines.append("- None")

    (run_dir / "README.md").write_text("\n".join(lines) + "\n")


def run_scenario(
    scenario: Scenario,
    *,
    run_dir: Path,
    agent: str,
    model: str | None,
    opencode_command: str,
    generation_timeout: int,
    review_timeout: int,
    max_fix_iterations: int,
) -> dict[str, Any]:
    scenario_dir = run_dir / scenario.slug
    scenario_dir.mkdir(parents=True, exist_ok=False)

    diagram_path = scenario_dir / f"{scenario.slug}.excalidraw"
    png_path = scenario_dir / f"{scenario.slug}.png"

    generation_prompt = format_generation_prompt(scenario, scenario_dir)
    record_generation_prompt(scenario, scenario_dir, generation_prompt)

    generation_session_id, _generation_export, generation_text = run_opencode_message(
        message=generation_prompt,
        artifact_dir=scenario_dir,
        step_prefix="generation",
        timeout=generation_timeout,
        agent=agent,
        model=model,
        opencode_command=opencode_command,
    )

    if not diagram_path.exists():
        raise HarnessError(
            f"Expected diagram artifact was not created: {diagram_path}. "
            "Check generation-response.txt for the model output."
        )

    render_diagram(diagram_path, png_path, scenario_dir)

    review_session_id, review_payload, _review_text = run_review(
        scenario=scenario,
        scenario_dir=scenario_dir,
        diagram_path=diagram_path,
        png_path=png_path,
        timeout=review_timeout,
        agent=agent,
        model=model,
        opencode_command=opencode_command,
        step_prefix="review-0",
    )

    latest_review_session_id = review_session_id
    latest_fix_text = ""
    for iteration in range(1, max_fix_iterations + 1):
        blocker_defects = review_payload.get("blocker_defects", [])
        if not blocker_defects:
            break
        latest_fix_text = run_fix_iteration(
            scenario=scenario,
            scenario_dir=scenario_dir,
            diagram_path=diagram_path,
            png_path=png_path,
            review_payload=review_payload,
            iteration=iteration,
            timeout=generation_timeout,
            agent=agent,
            model=model,
            opencode_command=opencode_command,
        )
        render_diagram(diagram_path, png_path, scenario_dir)
        latest_review_session_id, review_payload, _review_text = run_review(
            scenario=scenario,
            scenario_dir=scenario_dir,
            diagram_path=diagram_path,
            png_path=png_path,
            timeout=review_timeout,
            agent=agent,
            model=model,
            opencode_command=opencode_command,
            step_prefix=f"review-{iteration}",
        )

    (scenario_dir / "review.json").write_text(json.dumps(review_payload, indent=2))
    write_taste_review_stub(scenario, scenario_dir, png_path)

    summary_row = build_summary_row(
        scenario=scenario,
        scenario_dir=scenario_dir,
        generation_session_id=generation_session_id,
        review_session_id=latest_review_session_id,
        review_payload=review_payload,
    )
    summary_row["generation_response"] = generation_text
    summary_row["latest_fix_response"] = latest_fix_text
    return summary_row


def main() -> int:
    args = parse_args()
    scenarios = load_scenarios()
    scenario_map = {scenario.slug: scenario for scenario in scenarios}

    if args.list:
        for scenario in scenarios:
            print(f"{scenario.slug}: {scenario.title}")
        return 0

    ensure_command_available(args.opencode_command)
    ensure_command_available("uv")

    selected: list[Scenario]
    if args.scenarios:
        missing = [slug for slug in args.scenarios if slug not in scenario_map]
        if missing:
            raise HarnessError(f"Unknown scenario(s): {', '.join(missing)}")
        selected = [scenario_map[slug] for slug in args.scenarios]
    else:
        selected = scenarios

    run_dir = make_run_dir(args.output_root, args.run_label)
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(REPO_ROOT),
        "opencode_command": args.opencode_command,
        "opencode_config": build_opencode_env().get("OPENCODE_CONFIG"),
        "opencode_config_dir": build_opencode_env().get("OPENCODE_CONFIG_DIR"),
        "agent": args.agent,
        "model": args.model,
        "selected_scenarios": [scenario.slug for scenario in selected],
    }
    (run_dir / "run.json").write_text(json.dumps(metadata, indent=2))

    for scenario in selected:
        started = time.time()
        try:
            row = run_scenario(
                scenario,
                run_dir=run_dir,
                agent=args.agent,
                model=args.model,
                opencode_command=args.opencode_command,
                generation_timeout=args.generation_timeout,
                review_timeout=args.review_timeout,
                max_fix_iterations=args.max_fix_iterations,
            )
            row["duration_seconds"] = round(time.time() - started, 2)
            rows.append(row)
            print(f"PASS {scenario.slug} -> {run_dir / scenario.slug}")
        except Exception as exc:  # noqa: BLE001
            failures.append({"slug": scenario.slug, "error": str(exc)})
            print(f"FAIL {scenario.slug}: {exc}", file=sys.stderr)
            if not args.keep_going:
                break

    write_run_summary(run_dir, rows, failures)
    print(f"Artifacts written to {run_dir}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HarnessError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
