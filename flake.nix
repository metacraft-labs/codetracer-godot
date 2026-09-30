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

    # The CodeTracer trace writer the GDScript recorder links, PINNED: the
    # revision is the one flake.lock records, and it is the only declaration of
    # it. `modules/gdscript/SCsub` links the archive built from exactly this
    # source (`ct-trace-writer` below, exported to the dev shell), and refuses a
    # vendored archive stamped with any other revision. Bump it with
    #   nix flake update codetracer-trace-format-nim
    # and re-vendor with scripts/vendor-trace-writer.sh.
    codetracer-trace-format-nim.url = "github:metacraft-labs/codetracer-trace-format-nim/dev";
  };

  outputs =
    {
      self,
      nixpkgs,
      codetracer-trace-format-nim,
    }:
    let
      systems = [
        "aarch64-darwin"
        "x86_64-darwin"
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f system);

      writerRev = codetracer-trace-format-nim.rev;

      # The static C-ABI writer archive, from the pinned source, with the
      # writer's own pinned Nim toolchain and Nim dependencies (its flake's
      # inputs), so this is the same compiler and the same `requires` the
      # writer's repository builds and tests with. The command is the writer's
      # `buildStaticLib` plus `-d:useMalloc`: the engine calls the writer from
      # its own worker threads, and with Nim's per-thread heaps the main
      # thread's close frees memory owned by a worker that has already exited
      # (a crash at exit, measured with gf_threads.gd).
      ctTraceWriterFor =
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          w = codetracer-trace-format-nim.inputs;
          nim = w.codetracer-toolchains.packages.${system}.nim-2_2;
        in
        pkgs.stdenv.mkDerivation {
          pname = "codetracer-trace-writer-static";
          version = builtins.substring 0 12 writerRev;
          src = codetracer-trace-format-nim;
          nativeBuildInputs = [ nim ];
          buildInputs = [ pkgs.zstd ];
          buildPhase = ''
            runHook preBuild
            export HOME=$TMPDIR
            nim c --app:staticlib --mm:arc --noMain -d:release -d:useMalloc \
              --nimMainPrefix:codetracerTraceWriter --passC:-fPIC \
              --hints:off --nimcache:$TMPDIR/nimcache \
              -p:src --path:${w.nim-stew} --path:${w.nim-results} \
              -o:libcodetracer_trace_writer.a src/codetracer_trace_writer_ffi.nim
            runHook postBuild
          '';
          installPhase = ''
            runHook preInstall
            install -Dm644 libcodetracer_trace_writer.a $out/lib/libcodetracer_trace_writer.a
            install -Dm644 include/codetracer_trace_writer.h $out/include/codetracer_trace_writer.h
            echo ${writerRev} > $out/rev
            runHook postInstall
          '';
        };
    in
    {
      packages = forAll (system: {
        ct-trace-writer = ctTraceWriterFor system;
        # The reader at the SAME revision, for the verification scripts.
        ct-print = codetracer-trace-format-nim.packages.${system}.default;
      });

      devShells = forAll (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          ctTraceWriter = ctTraceWriterFor system;
          ctPrint = codetracer-trace-format-nim.packages.${system}.default;
          writerEnv = ''
            export CT_TRACE_WRITER_DIR="${ctTraceWriter}"
            export CT_TRACE_WRITER_REV="${writerRev}"
            export CT_PRINT="${ctPrint}/bin/ct-print"
          '';
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
                shellHook = writerEnv + ''
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
                shellHook = writerEnv + ''
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
