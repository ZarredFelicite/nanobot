{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
    # Python 3.13
    python313
    python313Packages.pip

    # Fast Python package manager
    uv

    # Node.js 22+
    nodejs_22

    # Build dependencies
    pkg-config
    openssl
  ];

  shellHook = ''
    # Create venv with uv if it doesn't exist
    if [ ! -d .venv ]; then
      echo "Creating Python venv with uv..."
      uv venv --python python3.13 .venv
    fi

    source .venv/bin/activate

    # Sync deps if pyproject.toml is newer than venv marker
    if [ pyproject.toml -nt .venv/.synced ] 2>/dev/null || [ ! -f .venv/.synced ]; then
      echo "Syncing dependencies..."
      uv pip install -e ".[dev]"
      touch .venv/.synced
    fi

    echo "nanobot dev environment ready (Python $(python --version 2>&1 | cut -d' ' -f2))"
  '';
}
