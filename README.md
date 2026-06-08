# tbi

GitHub release tarball installer.

```sh
pip install -e .
tbi install getsops/sops
tbi install gh --tag v2.63.2 --unattended
tbi aliases show
```

Environment:

- `TBI_INSTALL_DIR` defaults to `~/.local/bin`
- `TBI_CACHE_DIR` defaults to `~/.cache/tbi`
- `GITHUB_PAT` authenticates GitHub API calls
