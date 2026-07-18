# AstrBot NewsFlow Plugin Archive

This directory is the source and deployment directory of the NewsFlow AstrBot plugin. Its public Git repository is an archive of the AstrBot integration layer, not a standalone installable plugin.

Core application source: [Shuyuxu211/NewsFlow](https://github.com/Shuyuxu211/NewsFlow).

## Runtime Boundary

- This plugin owns AstrBot commands, scheduled push delivery, plugin configuration, and the Plugin Page.
- The NewsFlow core application is mounted into Docker at `/NewsFlow` and owns collection, filtering, SQLite storage, newsletter generation, and the standalone console.
- Core filtering also owns article/event deduplication, story and source caps, topic quotas, and published-event memory; the adapter must not repeat text-based deduplication after translation.
- The plugin renders newsletter HTML with Playwright in the AstrBot container. Chromium uses the shared persistent directory `/AstrBot/data/playwright_browsers` and is installed only when the required revision is missing.
- The active Compose definition builds `astrbot-playwright:<AstrBot版本>` from `F:\AstrBot\docker\astrbot-playwright.Dockerfile`; it supplies Chromium runtime libraries and Chinese fonts without embedding a browser download in the image.
- Plugin data is written through `StarTools.get_data_dir("astrbot_plugin_newsflow")`, never into this repository directory.

## AI Compatibility

- DeepSeek is the only production-validated backend for the current filtering and translation pipeline.
- The prompt, JSON contract, batch size, output token budget, disabled-thinking parameter, and request pacing are tuned against DeepSeek behavior.
- `openai`, `qwen`, `gemini`, `zhipu`, and `groq` remain experimental compatibility paths. Their rate limits, context limits, safety policies, and structured-output behavior are not covered by a maintained regression matrix.
- A public release must describe itself as DeepSeek-first unless a provider conformance suite is added.
## Development

1. Edit plugin source in this directory.
2. For Python, `metadata.yaml`, or `_conf_schema.json` changes, use the AstrBot WebUI plugin menu and select `Reload plugin`.
3. For existing files in `pages/dashboard/`, refresh the Plugin Page.
4. Changes under `/NewsFlow/src` currently require an AstrBot restart because they are imported as top-level `src.*` modules outside the plugin reload namespace.

Do not use the AstrBot WebUI update action on this development checkout. The updater replaces the complete plugin directory from a ZIP archive. Use Git after the plugin repository has a remote, then reload the plugin.

## Public Archive Boundary

This checkout is not a standalone distributable plugin. It relies on the external `/NewsFlow` core mount and the AstrBot container's Playwright/Chromium runtime. Do not install it through the AstrBot marketplace or set a public `repo` in `metadata.yaml`.

The public repository exists only to archive this adapter's source history alongside the private local deployment. It does not include API keys, AstrBot plugin configuration, session targets, databases, rendered images, or runtime data.

A future independent distribution must bundle the core modules and a supported local renderer. AstrBot installs and updates plugins from a repository root, so that distribution must be a dedicated repository rather than point at a NewsFlow core repository subdirectory.
