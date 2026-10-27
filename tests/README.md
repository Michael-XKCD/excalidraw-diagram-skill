# Live Baseline Suite

This directory contains the live end-to-end baseline harness for the Excalidraw skill.

The suite is intentionally taste-oriented before it is regression-oriented:

- It asks OpenCode to generate diagrams using the local skill materials in `SKILL.md` and `references/`.
- It re-renders the final `.excalidraw` output with the local renderer.
- It asks OpenCode to review the rendered PNG and propose improvements.
- It saves artifacts so a human can rank favorites and record likes/dislikes.

## Why this exists

The first objective is to establish a baseline of diagrams the user actually likes.
That baseline will later become the acceptance gate for renderer hardening work,
especially removing the remote `esm.sh` dependency from `references/render_template.html`.

## Run the suite

By default the harness now uses the same `opencode-work` config as your `ocw` alias when `~/.config/opencode-work/` is present. If you want to be explicit, pass `--opencode-command opencode` and the harness will still inject the `ocw` config environment automatically.

Run the full baseline set:

```bash
python3 tests/live_baseline.py
```

Run only the anchor scenario:

```bash
python3 tests/live_baseline.py --scenario ai-streaming-architecture
```

List available scenarios:

```bash
python3 tests/live_baseline.py --list
```

Artifacts are written under `artifacts/live-baseline/<run-id>/`.

## Artifacts per scenario

Each scenario directory includes:

- the prompt used for generation
- OpenCode event streams and exported session JSON
- the generated `.excalidraw` file
- the rendered `.png`
- the agent review JSON
- a `taste-review.md` stub for human ranking and notes

## Review workflow

After a run:

1. Open each rendered PNG.
2. Rank your favorites across the set.
3. Fill in the `taste-review.md` files with what you like and dislike.
4. Use those notes to turn taste into explicit baseline preferences.

## Notes

- The suite is live-only: every run calls the real OpenCode CLI.
- It assumes `opencode` and `uv` are installed and working.
- If `~/.config/opencode-work/` exists, the harness automatically uses that config so runs match `ocw` behavior.
- It does not try to enforce pixel-perfect outputs yet; it captures reviewable baselines.
