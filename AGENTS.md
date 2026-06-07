# Camera-Based RGBWW Optimizer Codex Instructions

## Language

- Reply in Chinese by default unless the user explicitly asks for another language.
- Write implementation plans in Chinese by default.

## Project Scope

- This is a local-first camera-based RGBWW optimizer project.
- The default target ESPHome RGBWW device name is `bedroom-rgbww-strip`.
- The attached fixture is a PWM RGBWW strip. The first milestone is proving independent control over all five physical channels.
- The later calibration workflow uses a camera, black/white/18% gray cards, a 24-color chart, and an Aputure Amaran Ray120c reference light at 100% brightness.

## Development Model

- Treat Python as the planned implementation language for optimizer and calibration tooling.
- Keep Python implementation modules under `src/camera_based_rgbww_optimizer/`: `control/` for device control, `optimization/` for measurement and calibration workflows, `utils/` for shared helpers/post-processing, and `interaction/` for local UI tools.
- Do not add root-level CLI compatibility wrappers. Use `python -m camera_based_rgbww_optimizer...` module commands and document them in `docs/README.md`.
- The project uses a minimal `pyproject.toml`; install locally with `.venv/bin/python -m pip install -e .`.
- Prefer local execution. Use Docker only when a concrete dependency requires it; if Docker is needed, use Colima and prefer `docker-compose`.
- Put all temporary data, probe outputs, decoded images, scratch virtual environments, and other disposable artifacts under this repository's `tmp/` directory. Do not write project temporary data to the system `/tmp`.

## Documentation

- Keep `docs/README.md` as the project source of truth for hardware assumptions, calibration decisions, validation workflow, and open questions.
- When behavior or calibration strategy changes, update `docs/README.md` or the relevant project skill in `.agents/skills/`.
- When a reusable project-specific method emerges, update the project skill under `.agents/skills/`.

## Implementation Quality

- Before changing behavior that touches device control, camera measurement, color conversion, optimizer solving, persistence, or shared state, inspect existing docs and implementations for a shared convention first.
- Add or update meaningful tests for functional changes unless the change is purely mechanical or documentation-only.
- Cover real behavior and edge cases; avoid tests that only execute code without checking outcomes.
- Run relevant checks before finishing when the environment permits it. If checks cannot run, explain the blocker.

## Safety

- Preserve user work. Do not remove, reset, or revert changes unless the user explicitly asks.
- Do not commit, push, or create pull requests unless explicitly requested.
- Do not send destructive or high-brightness device-control sweeps without an explicit bounded range and a clear recovery path.
- When testing RGBWW channel output, keep initial PWM values low and ramp only after confirming channel order and thermal behavior.
- All scripts that can leave the ESPHome RGBWW fixture on must explicitly turn the light off before exiting; dedicated light-on/control scripts are the only exception.
