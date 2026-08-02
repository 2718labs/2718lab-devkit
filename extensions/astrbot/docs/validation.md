# Validation Boundaries

`devkit-astrbot validate <plugin-directory>` reads plugin files as text and
parses Python with the standard-library `ast` module. It never imports the
plugin or the AstrBot runtime.

Errors cover missing `main.py` and `metadata.yaml`, malformed metadata names
and versions, unsupported bot-framework imports, multiple `Star` subclasses,
the `__del__` lifecycle trap, stacked command filters, response handlers that
return values instead of yielding, invalid config schema types, and forbidden
AstrBot requirements.

Warnings identify missing update metadata, a missing cleanup method, handlers
without a reply, and incompatible use of `astrbot.api.web`. Static validation
is deliberately not a substitute for an AstrBot integration test in a clean
host environment.
