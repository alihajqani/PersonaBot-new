# PersonaBot — notes for AI assistants

Synthetic survey respondents for online questionnaires. One codebase; each
questionnaire is data in `surveys/<id>/`. Read `README.md` for usage and
`docs/KNOWLEDGE.md` before touching a provider — most selectors and waits exist
because of a specific live-form failure recorded there.

## Rules of the house

- **Never copy the repository for a new form.** Add `surveys/<id>/`
  (`personabot new-survey <id>`). Survey-specific values belong in
  `survey.yaml` (`form:`, `prompt:`), never in `.env` or Python.
- Three layers: `Settings` (.env, deployment only) · `SurveySpec`
  (survey.yaml) · Jinja templates (`prompts/base/<lang>/` + survey overrides).
  `Workspace` binds a spec to settings and owns all paths.
- `CircuitError` may only be raised before anything is POSTed; it triggers a
  retry on another route. After filling starts, raise `SubmissionError`.
- Porsline probe and click must share the JS helpers (`_HELPERS`,
  `scopeFor`) — what is read must be what is clicked.
- No survey content in Python; no bare `except`; no network in tests.
- Update `CHANGELOG.md` under `[Unreleased]` with every user-visible change.

## Commands

```bash
make install && make test            # 530+ tests; every survey is validated
.venv/bin/python -m personabot.cli --survey <id> validate-config
```
