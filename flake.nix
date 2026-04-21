{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    systems.url = "github:nix-systems/default";
    git-hooks = {
      url = "github:cachix/git-hooks.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      systems,
      git-hooks,
      ...
    }:
    let
      forEachSystem = nixpkgs.lib.genAttrs (import systems);
      pkgsFor = system: nixpkgs.legacyPackages.${system};
    in
    {
      checks = forEachSystem (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          pre-commit-check = git-hooks.lib.${system}.run {
            src = ./.;
            hooks = {
              nixfmt.enable = true;
              ruff.enable = true;
              ty = (
                { config, lib, ... }:
                {
                  enable = true;
                  package = pkgs.ty;
                  entry = "${lib.getExe config.package} check --python ${
                    pkgs.python3.withPackages (_: self.packages.${system}.default.dependencies)
                  }/bin/python3";
                  types = [ "python" ];
                }
              );
            };
          };
        }
      );

      formatter = forEachSystem (
        system:
        let
          pkgs = pkgsFor system;
          inherit (self.checks.${system}.pre-commit-check.config) package configFile;
        in
        pkgs.writeShellScriptBin "pre-commit-run" ''
          ${pkgs.lib.getExe package} run --all-files --config ${configFile}
        ''
      );

      packages = forEachSystem (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = pkgs.python3Packages.buildPythonApplication {
            pname = "transcribe-meeting";
            version = "0.1.0";
            src = ./.;
            pyproject = true;
            build-system = [ pkgs.python3Packages.setuptools ];
            dependencies = with pkgs.python3Packages; [
              numpy
              websockets
            ];
            makeWrapperArgs = [
              "--prefix"
              "PATH"
              ":"
              (pkgs.lib.makeBinPath [
                pkgs.pipewire
                pkgs.v4l-utils
              ])
            ];
          };
        }
      );

      devShells = forEachSystem (
        system:
        let
          pkgs = pkgsFor system;
          inherit (self.checks.${system}.pre-commit-check) shellHook enabledPackages;
        in
        {
          default = pkgs.mkShell {
            inherit shellHook;
            inputsFrom = [ self.packages.${system}.default ];
            packages = enabledPackages;
          };
        }
      );

      homeManagerModules.default = import ./module.nix { inherit self; };
    };
}
