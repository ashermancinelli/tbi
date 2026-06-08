# tbi

GitHub release tarball installer.

```sh
pip install -e .
tbi install getsops/sops
tbi install gh --tag v2.63.2 --unattended
tbi install bat lazygit zellij
tbi install --prefix ~/.local/bin bat
tbi aliases show
```

Environment:

- `TBI_INSTALL_DIR` defaults to `~/.local/bin`; `install --prefix <bin dir>` overrides it
- `TBI_CACHE_DIR` defaults to `~/.cache/tbi`
- `GITHUB_PAT` authenticates GitHub API calls

Aliases are YAML mappings. Later files override earlier ones:

```yaml
aliases:
  lazygit: jesseduffield/lazygit
```

Load order:

- bundled defaults
- `~/.config/tbi/aliases.yaml`
- `./tbi.yaml`
- `./.tbi.yaml`
