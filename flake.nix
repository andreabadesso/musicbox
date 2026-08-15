{
  description = "musicbox: an event-shaped HTTP API in front of Music Assistant";

  # ── One input, on purpose ───────────────────────────────────────────────────
  # No flake-utils, no flake-parts, no `systems`. Every extra input is another
  # tarball the *Pi* has to fetch at eval time, because this box deploys by
  # rsyncing the config over and running `nixos-rebuild` locally on the Pi
  # rather than building on a fat machine and pushing a closure. The
  # `eachSystem` helper below is nine lines; flake-utils is not worth a network
  # round trip on a link that is also carrying the hackathon.
  #
  # Consumers should set `inputs.musicbox.inputs.nixpkgs.follows = "nixpkgs"`.
  # Nothing here needs a specific nixpkgs: the whole dependency set (fastapi,
  # uvicorn, aiohttp, bluez-alsa, snapcast) is stable across 25.11 and
  # unstable, and following the consumer's pin means musicbox adds exactly zero
  # extra fetches to their rebuild.
  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "aarch64-linux" "x86_64-linux" "aarch64-darwin" ];

      # x86_64-darwin is deliberately absent: nothing in this project has ever
      # run on an Intel Mac and claiming the platform only invites a broken
      # `nix flake check` from someone who has one.
      eachSystem = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = eachSystem (pkgs: rec {
        musicbox = pkgs.callPackage ./nix/package.nix { };
        default = musicbox;
      });

      # `import ./nix/module.nix self` keeps `self` in scope so the module's
      # `package` option can default to the flake's own build for whatever
      # system it is being evaluated on. It is a plain function value, so
      # `nix flake check` on the Mac never evaluates the Linux-only guts of it.
      nixosModules.musicbox = import ./nix/module.nix self;
      nixosModules.default = self.nixosModules.musicbox;

      devShells = eachSystem (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: with ps; [
              fastapi
              uvicorn
              aiohttp
              mcp
              # Dev-only, not runtime deps: httpx drives fastapi's TestClient
              # and pytest runs the suite. Keeping them out of nix/package.nix
              # keeps them out of the Pi's closure.
              httpx
              pytest
              pytest-asyncio
            ]))
            pkgs.nixpkgs-fmt
            # Useful for poking the live MA box from the dev shell without
            # writing a client: `websocat ws://pi5:8095/ws` is not cached, but
            # curl covers POST /api and GET /info, which is most of it.
            pkgs.curl
            pkgs.jq
          ];
        };
      });

      # A NixOS eval of the module, so a typo in an option type or a missing
      # `mkIf` is caught by `nix flake check` instead of by the Pi at 3am.
      # Only declared for the Linux systems: `nix flake check` builds the
      # checks for the *host* system only, so this is skipped on the Mac and
      # runs wherever CI or the Pi evaluates it.
      checks = nixpkgs.lib.genAttrs [ "aarch64-linux" "x86_64-linux" ] (system: {
        module-eval = (nixpkgs.lib.nixosSystem {
          inherit system;
          modules = [
            self.nixosModules.musicbox
            ({ ... }: {
              boot.loader.grub.enable = false;
              fileSystems."/" = { device = "/dev/disk/by-label/nixos"; fsType = "ext4"; };
              system.stateVersion = "25.11";
              services.musicbox = {
                enable = true;
                player = "musicbox";
                # Both token paths are set so the check exercises the
                # LoadCredential wiring, and as quoted STRINGS so it also
                # exercises the assertion that rejects nix path values (which
                # would copy the secret into the store). They do not have to
                # exist: nothing here starts the unit.
                tokenFile = "/etc/musicbox/token";
                maTokenFile = "/etc/musicbox/ma-token";
                musicAssistant.enable = true;
                bluetoothAudio = {
                  enable = true;
                  speakerMac = "AA:BB:CC:DD:EE:FF";
                };
              };
            })
          ];
        }).config.system.build.toplevel;
      });

      formatter = eachSystem (pkgs: pkgs.nixpkgs-fmt);
    };
}
