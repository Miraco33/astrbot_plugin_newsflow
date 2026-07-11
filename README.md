# AstrBot NewsFlow Plugin Archive

This directory is the source and deployment directory of the NewsFlow AstrBot plugin. Its public Git repository is an archive of the AstrBot integration layer, not a standalone installable plugin.

Core application source: [Shuyuxu211/NewsFlow](https://github.com/Shuyuxu211/NewsFlow).

## Runtime Boundary

- This plugin owns AstrBot commands, scheduled push delivery, plugin configuration, and the Plugin Page.
- The NewsFlow core application is mounted into Docker at `/NewsFlow` and owns collection, filtering, SQLite storage, newsletter generation, the standalone console, and local Chrome image rendering.
- Plugin data is written through `StarTools.get_data_dir("astrbot_plugin_newsflow")`, never into this repository directory.

## Development

1. Edit plugin source in this directory.
2. For Python, `metadata.yaml`, or `_conf_schema.json` changes, use the AstrBot WebUI plugin menu and select `Reload plugin`.
3. For existing files in `pages/dashboard/`, refresh the Plugin Page.
4. Changes under `/NewsFlow/src` currently require an AstrBot restart because they are imported as top-level `src.*` modules outside the plugin reload namespace.

Do not use the AstrBot WebUI update action on this development checkout. The updater replaces the complete plugin directory from a ZIP archive. Use Git after the plugin repository has a remote, then reload the plugin.

## Public Archive Boundary

This checkout is not a standalone distributable plugin. It relies on the external `/NewsFlow` core mount and the host-local Chrome rendering service. Do not install it through the AstrBot marketplace or set a public `repo` in `metadata.yaml`.

The public repository exists only to archive this adapter's source history alongside the private local deployment. It does not include API keys, AstrBot plugin configuration, session targets, databases, rendered images, or runtime data.

A future independent distribution must bundle the core modules and a supported local renderer. AstrBot installs and updates plugins from a repository root, so that distribution must be a dedicated repository rather than point at a NewsFlow core repository subdirectory.
