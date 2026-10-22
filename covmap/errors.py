"""Errors the CLI reports as a refusal (docs/SPEC.md "CLI", Errors: exit 2, no trace)."""


class Refusal(ValueError):
    """The command refuses its input: a file of another case, a malformed event log, an unsafe
    ``segment --reuse``. Not a crash."""
