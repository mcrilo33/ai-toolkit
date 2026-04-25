# Create README

Create a comprehensive, well-structured README.md for a project.

## When to use

**Trigger phrases:**
- "Create a README..."
- "Write a README..."
- "Bootstrap a README..."
- "Init a README..."

**Do NOT use when:**
- Updating an existing README (use `generate-docs` instead)
- Writing non-README documentation

## Workflow

1. **Explore the project** — read the full workspace structure, entry points,
   configs, and existing docs to understand purpose, stack, and usage.
2. **Fetch reference READMEs** for structural inspiration:
   - https://raw.githubusercontent.com/Azure-Samples/serverless-chat-langchainjs/refs/heads/main/README.md
   - https://raw.githubusercontent.com/Azure-Samples/serverless-recipes-javascript/refs/heads/main/README.md
   - https://raw.githubusercontent.com/sinedied/run-on-output/refs/heads/main/README.md
   - https://raw.githubusercontent.com/sinedied/smoke/refs/heads/main/README.md
3. **Draft the README** following the structure and constraints below.
4. **Review** for accuracy, broken links, and completeness.

## Structure

Use the following sections in order. Omit any section that doesn't apply.

1. **Header** — project name (with logo/icon if available), one-line
   description, and optional badges (CI, version, license).
2. **Overview** — 2-4 sentences explaining what the project does and why it
   exists.
3. **Features** — bullet list of key capabilities.
4. **Prerequisites** — required tools, runtimes, accounts.
5. **Getting started** — minimal steps to install and run.
6. **Usage** — common commands, API examples, or screenshots.
7. **Project structure** — brief tree or table of important directories/files.
8. **Configuration** — environment variables, config files, flags.
9. **Resources** — links to docs, related projects, or references.

## Constraints

- Use GFM (GitHub Flavored Markdown).
- Use GitHub admonition syntax (`> [!NOTE]`, `> [!WARNING]`, `> [!TIP]`)
  where appropriate.
- Follow the project's `markdown-style` rule for formatting.
- Keep it concise — prefer links to detailed docs over inline duplication.
- Do not use emojis excessively.
- Do **not** include LICENSE, CONTRIBUTING, CHANGELOG, or CODE_OF_CONDUCT
  sections — those belong in dedicated files.
- If a logo or icon exists in the repo, use it in the header.

## Tone

- Professional but approachable.
- Direct — lead with value, skip filler.
- Written for a developer discovering the project for the first time.
