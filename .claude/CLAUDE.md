<!-- @import /home/lab/workspace/.claude/CLAUDE.md -->

# Project-Specific Configuration

This file imports workspace-level configuration from `/home/lab/workspace/.claude/CLAUDE.md`.
All workspace rules apply. Project-specific rules below strengthen or extend them.

The workspace `/home/lab/workspace/.claude/` directory contains additional instruction files
(MERMAID.md, NOTEBOOK.md, DATASCIENCE.md, GIT.md, and others) referenced by CLAUDE.md.
Consult workspace CLAUDE.md and the .claude directory to discover all applicable standards.

## Mandatory Bans (Reinforced)

The following workspace rules are STRICTLY ENFORCED for this project:

- **No automatic git tags** - only create tags when user explicitly requests
- **No automatic version changes** - only modify version in package.json/pyproject.toml/etc. when user explicitly requests
- **No automatic publishing** - never run `make publish`, `npm publish`, `twine upload`, or similar without explicit user request
- **No manual package installs if Makefile exists** - use `make install` or equivalent Makefile targets, not direct `pip install`/`uv install`/`npm install`
- **No automatic git commits or pushes** - only when user explicitly requests

## Project Context

JupyterLab extension that adds a "Share Files" context menu item to the file browser, enabling users to generate shareable links for files and directories. The extension consists of a TypeScript frontend plugin (`src/index.ts`) and a Python server extension (`jupyterlab_share_files_extension/routes.py`).

- **Technology stack**: TypeScript (frontend), Python (server), JupyterLab 4, jupyter_server 2
- **Package name (npm)**: `jupyterlab_share_files_extension`
- **Package name (PyPI)**: `jupyterlab-share-files-extension`
- **Build system**: hatchling with hatch-jupyter-builder
- **Current version**: 0.1.0 (defined in `package.json`)

## Committed Artefacts

**MANDATORY**: Always commit both `package.json` and `package-lock.json` together. These files must be tracked in git.

## Makefile Version Tracking

**MANDATORY**: At the start of every session, compare the local `Makefile` version header against the canonical version at `private/jupyterlab/@utils/jupyterlab-extensions/Makefile`. If the canonical Makefile has a newer version number, update the local Makefile immediately by copying the canonical version. The version is declared on line 1 as `# Makefile for Jupyterlab extensions version X.Y`.

## Required Workspace Skills

The following workspace skills at `/home/lab/workspace/.claude/skills/` MUST be consulted when working on this project:

- **jupyterlab-extension** (`/home/lab/workspace/.claude/skills/jupyterlab-extension/SKILL.md`) - extension development guidelines, testing strategy, CI/CD workflows with jupyter-releaser, TypeScript compatibility, syntax highlighting, common caveats, and local development patterns
- **playwright** (`/home/lab/workspace/.claude/skills/playwright/SKILL.md`) - browser automation for screenshots and UI verification using Playwright MCP

## Journal Rules (Project-Specific)

- **APPEND ONLY**: New journal entries MUST be appended at the end of the file, never inserted between existing entries
- Entries maintain strict chronological order by position - the last entry in the file is always the most recent work
- Never reorder, move, or insert entries out of sequence
- The Stellars **journal plugin** is the canonical tool for this file: create via `/journal:create`, append via `/journal:update`, archive via `/journal:archive`. The `journal:journal` skill auto-triggers on any mention of "journal" and runs `journal-tools check` after every write
- Direct edits to `JOURNAL.md` are a last resort - prefer the plugin so modus secundis format, continuous numbering and append-only order are enforced automatically
