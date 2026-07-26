# Repository Guidelines

## Table Of Contents

- [Required Standards](#required-standards)
- [Scope](#scope)
- [Python](#python)
- [Verification](#verification)

## Required Standards

- `project-standards:aws-cloudformation-developer`
- `project-standards:docker-compose-developer`
- `project-standards:http-api-client-developer`
- `project-standards:kubernetes-developer`
- `project-standards:legacy-python-maintainer`
- `project-standards:project-documentation-developer`
- `project-standards:project-foundation`
- `project-standards:project-instruction-developer`
- `project-standards:project-standard-audit`
- `project-standards:project-standardize`
- `project-standards:pytest-developer`
- `project-standards:python-cli-developer`
- `project-standards:python-developer`
- `project-standards:python-logging-developer`
- `project-standards:python-retry-developer`
- `project-standards:react-ui-developer`
- `project-standards:rest-api-server-developer`
- `project-standards:runtime-config-developer`
- `project-standards:sqlalchemy-developer`
- `project-standards:submodule-developer`
- `project-standards:typescript-developer`
- `project-standards:zitadel-developer`
- `workflow-container-agent-tools:workflow-container-developer` applies to workflow-container runtime integration.

If one required provider skill is unavailable, continue read-only discovery only and do not mutate this repository until the provider is restored.

Active task pairs live only under the ignored `.spec/` root.

## Scope
- This repository owns the reusable browser runtime capability only.
- Shared workflow-container ecosystem authoring and code quality rules belong to `workflow-container-agent-tools:workflow-container-developer`.
- Do not add domain-specific or workflow-specific business logic.
- Keep the runtime boundary explicit: this repository owns Playwright execution and profiles, `vpn-runtime` owns VPN connectivity and SOCKS5 gateways, and callers own domain extraction behavior.
- The Playwright MCP runtime must expose one runtime-owned browser stack; consumers may select logical run-local profile names through the workflow contract but must not configure direct `@playwright/mcp`, direct `npx`, physical profile paths, profile-copy operations, or caller-owned browser flags as replacements for this stack.
- The router must treat physical profile plus the exact optional stable network proxy name supplied by the caller as the backend identity and must validate that name against the immutable platform-provided proxy map; it must not select or distribute proxy names.
- This repository must not own VPN config parsing, OpenVPN, WireGuard, `tun0`, SOCKS5 server lifecycle, provider connection slots, or VPN validation.

## Python
- Python code uses Python 3.14.
- Python code must be formatted with Black using target version `py314` and line length `120`.
- Public API, stable runtime boundaries, and non-trivial modules must have docstrings that describe real behavior.
- Runtime configuration and runtime state objects must use strict Pydantic models.
- Tests must use `pytest`.
- Tests must not verify instruction artifacts by checking that specific prose, headings, phrases, examples, files, or placement rules exist or do not exist. Instruction artifacts are verified by semantic reread or semantic audit, not by pytest assertions over text or instruction artifact paths.

## Verification
- Run `python -m pytest -q` after Python behavior changes.
- Run `python -m compileall browser_runtime` before handoff.
- Re-read `README.md` and `DESIGN.md` after documentation changes that affect runtime boundaries.
