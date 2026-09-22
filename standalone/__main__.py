from ..core.config.config import Settings, settings

# Load the standalone environment only at the explicit standalone entry point.
standalone_settings = Settings()
for name in Settings.model_fields:
    setattr(settings, name, getattr(standalone_settings, name))

from .cli.cli import cli

if __name__ == "__main__":
    cli()
