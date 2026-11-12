# Changelog

## 3.13.0

### Agent runtime

- Added bounded parallel child agents, stable instance identities, background execution, collection, cancellation, and durable result receipts.
- Enforced agent tool sets and argument schemas; preserved runtime-supplied arguments and task identity.
- Made conversation commits atomic and surfaced state corruption and write failures.
- Added local task ownership, safer process identity checks, cooperative stopping, and cleanup when a runtime worker loses its controller.
- Preserved full large tool outputs as files while bounding their model-context previews.
- Added run usage totals across parent and child agents and a rebuildable local usage ledger.
- Fixed model-client caching across roots and support for separate image/vision API credentials.

### Developer console and workspaces

- Docker starts the existing developer console on port 4260.
- Added five-role model API configuration: execution, thinking, compression, vision, and image generation.
- Retained the usage panel with time ranges, grouping, cache statistics, task totals, and optional model pricing.
- Added local shared workspaces with independent session records and configurable concurrency.
- Connected file uploads/downloads, human interactions, task history, stop/resume, and queued messages to the local runtime.
- Matched concurrent tool cards by call ID as well as agent identity.

### Packaging

- Python 3.10 or newer is required; package license metadata now matches the repository's GPL-3.0 license.
- Added a wheel-based Docker build for arm64 and amd64, with a shared Linux/Python 3.12 dependency lock.
- Rewrote the English and Chinese READMEs and the runtime, SDK, Docker, and usage guides.
- Added source and package CI across Python versions and platforms.

### Upgrading

Keep and back up `user_root` before replacing a container. Use the Docker command in the current README: the default entrypoint is the developer console and its container port is 4260. Configure model roles through the sidebar, or edit `config/llm_config.yaml` between runs.
