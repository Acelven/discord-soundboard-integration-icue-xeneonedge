# Widget: Compiled Package vs. Source

This project ships the iCUE widget in two forms. This document explains what
each one is, which to use, and how to go from source to a compiled package.

## The two forms

### `discord-soundboard.icuewidget` — the compiled package

This is the **ready-to-install** build. It is a single packaged file produced by
the iCUE Widget CLI from the source folder. Install this one in iCUE — it's what
you import to get the widget onto your XENEON EDGE.

You cannot meaningfully edit this file directly; it's the output of the build
step, analogous to a `.exe` compiled from code.

**Install:** in iCUE, open the widgets section, click **+**, and select this
file.

### `DiscordSoundboard/` (source) — the human-editable source

This is the **source folder** the package is built from. Edit these files to
change how the widget looks or behaves, then re-package (see below) to produce a
new `.icuewidget`.

```
DiscordSoundboard/
├── manifest.json        # widget metadata: id, name, version, target devices,
│                        #   interactive flag, minimum iCUE version
├── index.html           # the entire widget — layout, styling, and all logic
│                        #   (fetches sounds/status from the agent, renders the
│                        #   tile grid, handles taps, join/leave, tabs)
└── resources/
    └── icon.svg         # the widget's preview icon
```

Everything the widget does lives in `index.html`. There is no build tooling,
bundler, or dependency install for the source itself — it's plain HTML, CSS, and
vanilla JavaScript. The only tool involved is the packager that zips it into the
`.icuewidget` format iCUE accepts.

## Why two forms?

iCUE does not install raw folders — it installs the packaged `.icuewidget`
format. The compiled package is what the app consumes; the source is what a
human reads and edits. Shipping both means:

- Anyone who just wants the widget installs the `.icuewidget` and is done.
- Anyone who wants to understand, audit, or modify it reads the source folder.

## Building the package from source

The compiled `.icuewidget` is produced with Corsair's official iCUE Widget CLI.

### 1. Install the CLI (one time)

Requires Node.js.

```bash
npm install -g icuewidget-cli
```

### 2. Validate (optional but recommended)

Catches manifest problems before packaging.

```bash
icuewidget validate DiscordSoundboard
```

### 3. Package

```bash
icuewidget package DiscordSoundboard
```

This emits `discord-soundboard.icuewidget` — the installable package. Import
that in iCUE.

## Editing the widget

1. Edit files in `DiscordSoundboard/` (almost always just `index.html`).
2. Bump the `version` field in `manifest.json` so iCUE recognizes it as a new
   build.
3. Re-run `icuewidget package DiscordSoundboard`.
4. In iCUE, remove the old widget tile, delete the old widget from the widget
   list, then import the new package and re-add it. (A clean replace avoids iCUE
   running a stale cached copy.)

## Gotchas worth knowing

- **Doctype casing.** The first line must be `<!DOCTYPE html>` (uppercase). iCUE's
  importer rejects a lowercase `<!doctype html>` with a "Missing title element"
  error even though the CLI validator passes it.
- **Self-closing head tags.** `<meta>` and `<link>` tags in the head must be
  self-closed (`... />`) or the importer errors.
- **Interactive flag is required for touch.** For taps to register on the device,
  the manifest must include `"interactive": true` **and** the head must contain
  `<meta name="x-icue-interactive" />`. Without both, iCUE renders the widget as
  display-only and never routes touch into it.
- **`min_app_version`.** The manifest must include a `min_app_version` string
  (this build targets `5.47`) or validation fails.

## Requirements

- iCUE 5.47 or newer
- A device with an interactive LCD (e.g. Corsair XENEON EDGE)
- The soundboard **agent** running and reachable — the widget is only a
  front-end; it fetches sounds and triggers playback through the agent's HTTP
  API. Set the agent URL (and API key, if used) in the widget's settings.
