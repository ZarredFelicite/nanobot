{ pkgs ? import <nixpkgs> {} }:

pkgs.buildNpmPackage {
  pname = "nanobot-tui";
  version = "0.1.0";

  src = ./.;

  npmDepsHash = "";  # Run `nix-prefetch-npm-deps tui/package-lock.json` to fill

  nodejs = pkgs.nodejs_22;

  buildPhase = ''
    npx tsc
  '';

  installPhase = ''
    mkdir -p $out/lib/nanobot-tui $out/bin
    cp -r dist node_modules package.json $out/lib/nanobot-tui/

    cat > $out/bin/nanobot-tui <<WRAPPER
    #!/usr/bin/env bash
    exec ${pkgs.nodejs_22}/bin/node $out/lib/nanobot-tui/dist/main.js "\$@"
    WRAPPER
    chmod +x $out/bin/nanobot-tui
  '';

  meta = with pkgs.lib; {
    description = "Pi-TUI frontend for nanobot";
    license = licenses.mit;
    mainProgram = "nanobot-tui";
  };
}
