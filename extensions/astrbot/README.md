# 2718lab-devkit-astrbot

`2718lab-devkit-astrbot` is a standalone package for creating and statically
checking AstrBot plugins. Its runtime uses only the Python standard library.
Importing `devkit_astrbot` does not import AstrBot, start a plugin, discover an
AstrBot data directory, or access a network service.

Current release: `1.0.0-rc3`.

The shipped templates are inert text. They are written only after an explicit
`scaffold` command to a caller-selected directory.

## Development

```powershell
uv sync
uv run pre-commit install
uv run pytest
```

## Commands

```powershell
uv run devkit-astrbot --version
uv run devkit-astrbot scaffold astrbot_plugin_echo D:\work\plugins
uv run devkit-astrbot validate D:\work\plugins\astrbot_plugin_echo
```

`scaffold` refuses unsafe names and existing destination directories. Update
the generated author, repository, and user-facing content before publishing a
plugin.

## Validation

Validation is static: plugin code is parsed with `ast` and never imported or
executed. It checks the common structure, metadata, lifecycle, handler,
configuration-schema, dependency, and AstrBot web API compatibility hazards.
See [docs/validation.md](docs/validation.md) for the rule boundaries.
