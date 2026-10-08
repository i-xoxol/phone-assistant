# Contributing

Thanks for helping make this small service easier to run and understand.
Please keep changes focused: one clear problem, a concrete fix, and appropriate
verification. Read the architecture and design notes before changing audio flow.

## Local workflow

1. Fork and clone the repository.
2. Create a Python 3.11+ virtual environment.
3. Install `requirements-dev.txt`.
4. Run `python -m pytest -q`. No provider credentials are needed.
5. Make the change, add meaningful tests where behavior changes, and update the
   relevant guide.
6. Run the tests and `python -m scripts.audit_release` before opening a pull request.

Use fictional numbers in the US reserved `202-555-0100` through `202-555-0199`
range and invented conversation content in examples. Do not copy real call
results, pairing codes, personal endpoints, provider identifiers, or credentials.
The `.env`, `data/`, and `.venv/` directories must stay out of commits.

Network scripts may contact paid providers. `scripts.manual_call` places a real
phone call; never add it or any live provider checks to CI. Use mocks for tests.

## Pull requests

Describe the user-visible problem, what changes, and how you verified it. For
audio changes, specify which parts were tested with mocks and which were tested
on a real call. Do not claim telephone quality from a simulated stream alone.

Avoid new infrastructure unless the change requires it. The MVP deliberately
uses a single process and SQLite. Larger features should first discuss ownership,
authentication, cost, and compatibility with existing deployments in an issue.

## Reporting bugs

Include Python/package versions, whether voice tools and MCP are enabled, and
the application-owned error reason or Twilio error code. Redact identifiers,
numbers, URLs, names, and transcript contents. See `SECURITY.md` for sensitive
reports.
