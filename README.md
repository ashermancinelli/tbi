# tbi (Don't use this!!!)

A vibecoded ripoff of [gah](https://github.com/get-gah/gah/tree/master).
Installs github release tarballs.

Install the launcher:

```sh
mkdir -p ~/.local/bin
curl -fsSL https://raw.githubusercontent.com/ashermancinelli/tbi/refs/heads/main/tools/install.sh -o ~/.local/bin/tbi
chmod +x ~/.local/bin/tbi

# alternatively:
uvx --from git+https://github.com/ashermancinelli/tbi.git tbi --help
```

```sh
pip install -e .
tbi install getsops/sops
tbi install gh --tag v2.63.2 --unattended
tbi install bat lazygit zellij
tbi install --prefix ~/.local bat
tbi install --keep-temp neovim
tbi aliases show
```

Environment:

- `TBI_INSTALL_DIR` defaults to `~/.local/bin`; `install --prefix <prefix>` installs binaries into `<prefix>/bin`
- `TBI_CACHE_DIR` defaults to `~/.cache/tbi`
- `GITHUB_PAT` authenticates GitHub API calls (needed if you get rate-limited)

Flags:

- `install --keep-temp` keeps the download/extract work directory under `TBI_CACHE_DIR` for inspection

Aliases are YAML mappings. Later files override earlier ones:

```yaml
aliases:
  lazygit: jesseduffield/lazygit

install:
  neovim/neovim:
    bin: bin
    lib: lib
    share: share
```

`install` entries map paths inside the extracted archive to paths under the install prefix. This is for packages like Neovim that ship runtime files outside `bin`.

Load order:

- bundled defaults
- `~/.config/tbi/aliases.yaml`
- `./tbi.yaml`
- `./.tbi.yaml`
