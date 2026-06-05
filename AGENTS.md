# WLED-RGBWW Codex Instructions

## Language

- Reply in Chinese by default unless the user explicitly asks for another language.
- Write implementation plans in Chinese by default.

## Project Scope

- This is a local-first WLED RGBWW optimizer project.
- The target WLED device is `wled-bedroom.local`.
- The attached fixture is a PWM RGBWW strip. The first milestone is proving independent control over all five physical channels.
- The later calibration workflow uses a camera, black/white/18% gray cards, a 24-color chart, and an Aputure Amaran Ray120c reference light at 100% brightness.

## Development Model

- Treat Python as the planned implementation language for optimizer and calibration tooling.
- No package manager, virtual environment, or test runner has been selected yet. When one is introduced, document the exact commands in `docs/README.md`.
- Prefer local execution. Use Docker only when a concrete dependency requires it; if Docker is needed, use Colima and prefer `docker-compose`.

## Documentation

- Keep `docs/README.md` as the project source of truth for hardware assumptions, calibration decisions, validation workflow, and open questions.
- When behavior or calibration strategy changes, update `docs/README.md` or the relevant project skill in `.agents/skills/`.
- When a reusable project-specific method emerges, update `.agents/skills/wled-rgbww/SKILL.md`.

## Implementation Quality

- Before changing behavior that touches device control, camera measurement, color conversion, optimizer solving, persistence, or shared state, inspect existing docs and implementations for a shared convention first.
- Add or update meaningful tests for functional changes unless the change is purely mechanical or documentation-only.
- Cover real behavior and edge cases; avoid tests that only execute code without checking outcomes.
- Run relevant checks before finishing when the environment permits it. If checks cannot run, explain the blocker.

## Safety

- Preserve user work. Do not remove, reset, or revert changes unless the user explicitly asks.
- Do not commit, push, or create pull requests unless explicitly requested.
- Do not send destructive or high-brightness device-control sweeps without an explicit bounded range and a clear recovery path.
- When testing WLED channel output, keep initial PWM values low and ramp only after confirming channel order and thermal behavior.
