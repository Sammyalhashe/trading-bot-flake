{
  description = "A simple Coinbase trading bot";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        pythonDependencies = ps: with ps;
          [ requests pandas pyjwt cryptography pytest web3 websockets ];

        pythonEnv = pkgs.python3.withPackages pythonDependencies;

      in
      {
        packages.default = pkgs.stdenv.mkDerivation {
          name = "coinbase-trading-bot";
          src = ./.;

          buildInputs = [ pkgs.makeWrapper ];

          installPhase = ''
            mkdir -p $out/bin

            # Copy all python files and packages to bin
            cp $src/*.py $out/bin/
            cp -r $src/config $out/bin/config
            cp -r $src/core $out/bin/core
            cp -r $src/strategies $out/bin/strategies
            cp -r $src/executors $out/bin/executors

            # Rename main entry point
            mv $out/bin/trading_bot.py $out/bin/trading-bot
            chmod +x $out/bin/trading-bot
            wrapProgram $out/bin/trading-bot \
              --set PATH ${pkgs.lib.makeBinPath [ pythonEnv ]} \
              --set PYTHONPATH $out/bin

            # Rename report bot
            mv $out/bin/report_bot.py $out/bin/trading-report
            chmod +x $out/bin/trading-report
            wrapProgram $out/bin/trading-report \
              --set PATH ${pkgs.lib.makeBinPath [ pythonEnv ]} \
              --set PYTHONPATH $out/bin

            # Rename notify bot
            mv $out/bin/notify_telegram.py $out/bin/trading-notify
            chmod +x $out/bin/trading-notify
            wrapProgram $out/bin/trading-notify \
              --set PATH ${pkgs.lib.makeBinPath [ pythonEnv ]} \
              --set PYTHONPATH $out/bin

            # Transaction debugger
            mv $out/bin/debug_tx.py $out/bin/debug-tx
            chmod +x $out/bin/debug-tx
            wrapProgram $out/bin/debug-tx \
              --set PATH ${pkgs.lib.makeBinPath [ pythonEnv ]} \
              --set PYTHONPATH $out/bin
          '';
        };

        apps = {
          default = {
            type = "app";
            program = "${self.packages.${system}.default}/bin/trading-bot";
          };
          report = {
            type = "app";
            program = "${self.packages.${system}.default}/bin/trading-report";
          };
          notify = {
            type = "app";
            program = "${self.packages.${system}.default}/bin/trading-notify";
          };
          debug-tx = {
            type = "app";
            program = "${self.packages.${system}.default}/bin/debug-tx";
          };
        };

        devShells.default = pkgs.mkShell {
          buildInputs = [
            pythonEnv
            pkgs.python3Packages.pip
          ];
        };
      });
}
