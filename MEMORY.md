# Repository Memory

## Execution Environment

- Work is performed through a CLI-only agent session.
- The Browser skill and interactive browser backend are not available in this
  environment.
- Do not spend time attempting Browser setup for UI validation. Use FastAPI
  route/template tests, JavaScript syntax checks, static inspection, and
  CLI-accessible validation instead.
- Only reconsider browser validation when the user explicitly says the
  execution environment now provides an interactive browser.

## Recent Implementation Context

- The translation integration is provider-neutral and uses an
  OpenAI-compatible API.
- The NAS settings UI queries `GET /models` and presents the returned model IDs
  as a selection list.
- Legacy `LM_STUDIO_*` environment variables remain supported as fallbacks for
  the `OPENAI_COMPATIBLE_*` settings.
- The dashboard recent-job area uses a responsive card list with localized
  statuses, progress, error context, and a 20-job display limit.
- Job details show events newest-first in a severity-aware timeline.
