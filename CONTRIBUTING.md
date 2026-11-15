# Contributing

Thanks for helping! This project deliberately keeps runtime dependencies at
zero and supports Python 3.8+.

## Development

```bash
git clone https://github.com/wangzi5151/linux-file-bridge.git
cd linux-file-bridge
python3 -m pip install -e .
make test
```

There is no formatter requirement, but please keep the existing style:

- Standard library only in `lfb/`.
- Type hints where useful, no `from __future__` features beyond what Python 3.8
  supports.
- Expected failures should be `LfbError` subclasses with a stable `code`.
- New features need tests, ideally both unit and an integration path.
- Update `README.md`, `docs/API.md` and `CHANGELOG.md` for user-visible changes.

## Security-sensitive changes

Path resolution, symlink handling, authentication, TLS, and `exec` are
security-sensitive. Include tests for:

- `..` traversal and absolute paths;
- symlinks pointing inside and outside a root;
- read-only roots;
- upload length mismatch and size limits;
- auth failures and gateway token replacement.

Do not add a feature that silently follows a symlink outside a root.

## Pull requests

1. Describe the use case and the user-visible behavior.
2. Run `make test` and `python3 -m py_compile lfb/*.py`.
3. Keep commits focused; squash fixups if asked.
4. Do not commit tokens, generated configs, or private paths.
