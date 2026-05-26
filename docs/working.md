# Working Notes

## Repository Contract

Tracked files should be safe for public release. Runtime files are local-only:

- `config/launcher.yaml`
- `.env`
- `logs/`
- caches and build outputs

Use `config/launcher.example.yaml` and `.env.example` for fake examples. Keep private overlays outside the repository.

## Development Setup

```bash
uv venv
uv pip install -e '.[dev]'
```

Run the CLI with either entrypoint:

```bash
process-launcher start --config config/launcher.yaml
python -m process_launcher start --config config/launcher.yaml
```

## Release Checklist

- Search tracked public files for private paths, real emails, private domains, and secrets.
- Run the unit test suite.
- Run the package entrypoint smoke check.
- Confirm `config/launcher.yaml`, `.env`, and `logs/` are ignored.
- Confirm docs describe the private overlay pattern rather than embedding private recipes.
