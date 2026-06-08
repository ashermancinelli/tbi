# tbi

GitHub release tarball installer.

Install the launcher:

```sh
mkdir -p ~/.local/bin
curl -fsSL https://raw.githubusercontent.com/ashermancinelli/tbi/refs/heads/main/tools/install.sh -o ~/.local/bin/tbi
chmod +x ~/.local/bin/tbi
```

The launcher installs `uv` if needed, then runs the latest `tbi` from GitHub with `uvx`.

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
