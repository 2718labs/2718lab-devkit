# 2718lab Project Index

## Goal

Make the plugin-mediated development workflow use a deterministic project index
instead of repeatedly asking agents to browse whole repositories and plans.

## Scope

Index code, Markdown work packages, configuration, tests, evidence, diffs, and
checkpoints. Add automatic restore only for plugin-owned task worktrees.

## Direction

Use one immutable typed graph per workspace snapshot. Facts come from parsers and
hashes, never model summaries. Agents query bounded graph slices and exact source
windows. Existing files and workflow records remain authoritative.

## Risk Gate

Never restore an original workspace, overwrite drift, touch commits/remotes, or
follow symlinks/reparse points automatically.

## Done

Strict tasks are index-bound from planning through verification; rollback reuses
the old snapshot; existing non-strict workflows remain compatible.

