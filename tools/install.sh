#!/usr/bin/env bash

set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
	if command -v curl >/dev/null 2>&1; then
		curl -LsSf https://astral.sh/uv/install.sh | sh
	elif command -v wget >/dev/null 2>&1; then
		wget -qO- https://astral.sh/uv/install.sh | sh
	else
		echo "Error: install curl or wget first" >&2
		exit 1
	fi
fi

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

exec uvx --from git+https://github.com/ashermancinelli/tbi.git tbi "$@"
