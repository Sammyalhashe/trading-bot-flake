---
name: nix-flake-dev
description: Guidelines for working within a Nix flake environment. Use this skill when working on projects that use Nix flakes for dependency management and development shells.
---

# Nix Flake Development

## Overview
This skill provides mandates for working in a Nix flake environment. All development and dependency management MUST occur through Nix.

## Core Mandates
- **Development Shells**: Always use `nix develop` to enter the project's development environment.
- **Dependency Management**: NEVER install dependencies globally or via `pip install`, `npm install`, etc. instead, add them to the `flake.nix` file under the appropriate attribute (e.g., `pythonDependencies`, `buildInputs`).
- **Temporary Environments**: Any temporary tool or library needed for a task should be accessed using `nix shell -p <package>` or by adding it to the `devShells` in the flake.

## Workflow
1. **Adding a Dependency**: Modify `flake.nix` to include the new package.
2. **Updating the Flake**: Run `nix flake update` if needed.
3. **Entering the Shell**: Run `nix develop` to refresh the environment.
