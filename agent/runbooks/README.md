# Runbooks

This directory contains declarative runbook definitions for the agentic SRE executor.

## Format

Each runbook is a Markdown file with YAML frontmatter containing metadata, followed by structured sections:

- **Problem**: Description of the issue
- **Diagnostic Steps**: How to investigate the problem
- **Remediation Actions**: Actionable steps to fix the issue
- **Success Criteria**: How to verify the fix worked
- **Notes**: Additional context

## Action Format

Actions are defined with:
- `action_id`: Unique identifier for the action
- `description`: Human-readable description
- `command`: Kubernetes command template (supports `{namespace}`, `{pod}`, `{container}`, `{deployment}` placeholders)
- `conditions`: Requirements for the action to be applicable

## Current Runbooks

- `RB_IMAGEPULL.md`: ImagePullBackOff remediation
- `RB_CRASHLOOP.md`: CrashLoopBackOff remediation  
- `RB_OOM.md`: OOMKilled remediation
- `RB_CONTAINERCREATING.md`: ContainerCreating stuck remediation
- `RB_NODE_UNSCHEDULABLE.md`: Node unschedulable (cordon) remediation
- `RB_NODE_NOTREADY.md`: Node NotReady remediation

## Usage

Runbooks are loaded by the agent at runtime based on the `runbook_id` determined from alert labels. The agent will:

1. Load the appropriate runbook file
2. Query Postgres for similar past incidents
3. Execute actions (either from runbook or reuse successful past actions)
4. Record results in incident_events

