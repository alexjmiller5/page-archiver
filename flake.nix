{
  description = "Self-contained web page and screenshot capture";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, uv2nix, pyproject-nix, pyproject-build-systems }:
    let
      inherit (nixpkgs) lib;
      systems = [ "aarch64-darwin" "x86_64-darwin" "aarch64-linux" "x86_64-linux" ];
      forAllSystems = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # Load the uv workspace (pyproject.toml + uv.lock) and build an overlay of
      # all locked dependencies. Prefer prebuilt wheels (matters on darwin).
      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };


      mkPythonSet = pkgs:
        (pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python313; }).overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.default
            overlay
            (final: prev: {
              page-archiver = prev.page-archiver.overrideAttrs (old: {
                postPatch = (old.postPatch or "") + ''
                  mkdir -p src/page_archiver/assets
                  cp ${pkgs.callPackage ./nix/browser.nix { }}/* src/page_archiver/assets/
                '';
              });
              playwright = prev.playwright.overrideAttrs (old: {
                nativeBuildInputs = (old.nativeBuildInputs or [ ])
                  ++ lib.optionals pkgs.stdenv.hostPlatform.isLinux [ pkgs.autoPatchelfHook ];
                buildInputs = (old.buildInputs or [ ])
                  ++ lib.optionals pkgs.stdenv.hostPlatform.isLinux [ pkgs.stdenv.cc.cc.lib ];
              });
            })
          ]
        );
    in
    {
      packages = forAllSystems (pkgs:
        let
          environment = (mkPythonSet pkgs).mkVirtualEnv "page-archiver-env" workspace.deps.default;
        in
        {
          default = pkgs.runCommand "page-archiver" {
            nativeBuildInputs = [ pkgs.makeWrapper ];
            meta = {
              description = "Self-contained web page and screenshot capture";
              license = lib.licenses.agpl3Plus;
              mainProgram = "page-archiver";
              platforms = systems;
            };
          } ''
            mkdir -p $out/bin
            makeWrapper ${environment}/bin/page-archiver $out/bin/page-archiver \
              --set PLAYWRIGHT_NODEJS_PATH ${pkgs.nodejs}/bin/node \
              ${lib.optionalString pkgs.stdenv.hostPlatform.isLinux "--set-default PAGE_ARCHIVER_BROWSER_EXECUTABLE ${pkgs.chromium}/bin/chromium"}
          '';

        });

      checks = forAllSystems (pkgs: {
        install = pkgs.buildEnv {
          name = "page-archiver-install-check";
          paths = [ self.packages.${pkgs.stdenv.hostPlatform.system}.default pkgs.python314 ];
          pathsToLink = [ "/bin" ];
        };
      });

      homeModules.default = import ./nix/home-manager.nix self;

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.uv pkgs.ruff pkgs.just pkgs.python313 pkgs.bun ];
        };
      });

      formatter = forAllSystems (pkgs: pkgs.nixpkgs-fmt);
    };
}
