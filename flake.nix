{
  # CodeTracer engine fork (Godot 4.6.2-stable) — GDScript recorder (G2+).
  #
  # scons is declared HERE, in the repo that actually builds the patched
  # engine, on purpose: this fork is the only place a Godot-from-source build
  # happens, so its build tool belongs to its own flake rather than to an
  # ad-hoc `nix shell nixpkgs#scons`. `nix develop` gives a reproducible build
  # shell for the GDScript recorder work.
  #
  # macOS note: Godot's `platform/macos/detect.py` does a *native* build with
  # `clang`/`clang++` and resolves the SDK through `xcrun` (`-isysroot <sdk>`).
  # On this nix-managed workstation there is no Xcode toolchain — `clang` and
  # `xcrun` are already nix-provided (a clang cc-wrapper + an apple-sdk shim),
  # which is exactly how the prior G2 writer probe linked nix's libzstd and the
  # Security/CoreFoundation frameworks. We therefore give the darwin shell a
  # normal clang stdenv `mkShell` (so `clang`/`clang++` resolve to the nix
  # cc-wrapper that knows the apple-sdk) plus scons/python3/pkg-config and the
  # CTFS writer's link dep (zstd). Because zstd is a buildInput, the nix
  # cc-wrapper auto-adds its `-L`, so the `-lzstd` in modules/gdscript/SCsub
  # resolves; the Apple frameworks come from the apple-sdk the stdenv carries.
  description = "CodeTracer patched Godot engine (GDScript recorder) build shell";

  inputs = {
    # nixpkgs pinned to the workstation's cached flakehub weekly (the local
    # registry already resolves `nixpkgs` to this), so `nix develop` needs no
    # network fetch — plain `github:NixOS/nixpkgs` hit GitHub 429s here. The
    # concrete revision is pinned in flake.lock.
    nixpkgs.url = "https://flakehub.com/f/DeterminateSystems/nixpkgs-weekly/0.1";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "aarch64-darwin"
        "x86_64-darwin"
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f system);
    in
    {
      devShells = forAll (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          isDarwin = pkgs.stdenv.hostPlatform.isDarwin;

          # Common Godot-from-source build tooling. Godot vendors almost all
          # of its third-party libraries under thirdparty/, so the host needs
          # little beyond scons + a python3 + a C++ compiler.
          buildTools = [
            pkgs.scons
            pkgs.python3
            pkgs.pkg-config
          ];

          # Link-time libraries. These must be `buildInputs` (not `packages`)
          # so the nix cc-wrapper adds their `-L` to the link line:
          #   * zstd — the CTFS writer static lib links libzstd;
          #   * zlib — Godot's platform/macos/detect.py links `-lz`.
          # (nix's apple-sdk sysroot ships neither as a .tbd, so the linker
          # would otherwise fail with "library not found for -lz".)
          linkLibs = [
            pkgs.zstd
            pkgs.zlib
          ];

          # On Linux a from-source Godot build needs X11/GL/ALSA/pulse dev
          # libs; those are added below (not exercised in this macOS spike).
          shell =
            if isDarwin then
              # Default (clang) stdenv mkShell: `clang`/`clang++` resolve to the
              # nix cc-wrapper carrying the apple-sdk, matching this machine.
              pkgs.mkShell {
                nativeBuildInputs = buildTools;
                buildInputs = linkLibs;
                shellHook = ''
                  export PKG_CONFIG_PATH="${pkgs.zstd.dev}/lib/pkgconfig''${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
                  echo "codetracer-engine-godot dev shell (darwin): scons $(scons --version 2>/dev/null | sed -n 's/.*v\([0-9.]*\).*/\1/p' | head -n1); clang $(clang --version 2>/dev/null | sed -n '1s/.*version \([0-9.]*\).*/\1/p')"
                '';
              }
            else
              pkgs.mkShell {
                nativeBuildInputs = buildTools;
                buildInputs = linkLibs ++ [
                  pkgs.xorg.libX11
                  pkgs.xorg.libXcursor
                  pkgs.xorg.libXinerama
                  pkgs.xorg.libXrandr
                  pkgs.xorg.libXi
                  pkgs.xorg.libXext
                  pkgs.libGL
                  pkgs.alsa-lib
                  pkgs.libpulseaudio
                ];
                shellHook = ''
                  export PKG_CONFIG_PATH="${pkgs.zstd.dev}/lib/pkgconfig''${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
                '';
              };
        in
        {
          default = shell;
        }
      );
    };
}
