"""Telemetry pull-side: parse Claude session logs into spans and correlate cost.

Issue #22 (session-log parser + token/cost correlation) builds the *pull* half of
the workflow-observability dataset on top of Issue #21's frozen span schema
(``docs/telemetry-span-schema.md``). Everything here is read-only and 100% local:
session logs contain prompt content, so they are parsed on-machine and only
metadata / metrics are ever surfaced — never raw prompt, answer, or tool-output
text.
"""
