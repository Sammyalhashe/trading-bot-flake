{
  description = "A simple Coinbase trading bot";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    sops-nix = {
      url = "github:Mic92/sops-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, flake-utils, sops-nix }:
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
            cp -r $src/backtesting $out/bin/backtesting

            # Ensure everything in $out/bin is writable before wrapping
            chmod -R +w $out/bin

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

            # Backtesting tools
            chmod +x $out/bin/backtesting/backtest.py
            ln -s $out/bin/backtesting/backtest.py $out/bin/backtest
            wrapProgram $out/bin/backtesting/backtest.py \
              --set PATH ${pkgs.lib.makeBinPath [ pythonEnv ]} \
              --set PYTHONPATH $out/bin

            chmod +x $out/bin/backtesting/comprehensive_backtest.py
            ln -s $out/bin/backtesting/comprehensive_backtest.py $out/bin/comprehensive-backtest
            wrapProgram $out/bin/backtesting/comprehensive_backtest.py \
              --set PATH ${pkgs.lib.makeBinPath [ pythonEnv ]} \
              --set PYTHONPATH $out/bin

            chmod +x $out/bin/backtesting/download_historical_data.py
            ln -s $out/bin/backtesting/download_historical_data.py $out/bin/download-data
            wrapProgram $out/bin/backtesting/download_historical_data.py \
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
          backtest = {
            type = "app";
            program = "${self.packages.${system}.default}/bin/backtest";
          };
          comprehensive-backtest = {
            type = "app";
            program = "${self.packages.${system}.default}/bin/comprehensive-backtest";
          };
          download-data = {
            type = "app";
            program = "${self.packages.${system}.default}/bin/download-data";
          };
        };

        devShells.default = pkgs.mkShell {
          buildInputs = [
            pythonEnv
            pkgs.python3Packages.pip
            pkgs.sops
            pkgs.age
            (pkgs.writeShellScriptBin "load-trading-secrets" ''
              set -e
              SECRETS_FILE="secrets.yaml"
              if [[ ! -f "''${SECRETS_FILE}" ]]; then
                echo "ERROR: ''${SECRETS_FILE} not found in current directory" >&2
                exit 1
              fi
              # Output export statements for environment variables
              echo "export ETH_RPC_URL=''$(sops -d --extract '["eth_rpc_url"]' "''${SECRETS_FILE}" 2>/dev/null || echo \"\")"
              echo "export ETH_PRIVATE_KEY=''$(sops -d --extract '["eth_private_key"]' "''${SECRETS_FILE}" 2>/dev/null || echo \"\")"
              echo "export TELEGRAM_BOT_TOKEN=''$(sops -d --extract '["telegram_bot_token"]' "''${SECRETS_FILE}" 2>/dev/null || echo \"\")"
              # Generate coinbase API JSON file and output export
              COINBASE_ID=''$(sops -d --extract '["coinbase_api_id_clawdbot"]' "''${SECRETS_FILE}" 2>/dev/null || echo "")
              COINBASE_SECRET=''$(sops -d --extract '["coinbase_api_secret_clawdbot"]' "''${SECRETS_FILE}" 2>/dev/null || echo "")
              if [[ -n "''${COINBASE_ID}" && -n "''${COINBASE_SECRET}" ]]; then
                COINBASE_JSON_DIR="''${XDG_DATA_HOME:-''${HOME}/.local/share}/trading-bot"
                mkdir -p "''${COINBASE_JSON_DIR}"
                COINBASE_JSON_PATH="''${COINBASE_JSON_DIR}/cdb_api_key.json"
                cat > "''${COINBASE_JSON_PATH}" <<EOF
{
   "name": "''${COINBASE_ID}",
   "privateKey": "''${COINBASE_SECRET}"
}
EOF
                echo "export COINBASE_API_JSON=''${COINBASE_JSON_PATH}"
                echo "Coinbase API JSON written to ''${COINBASE_JSON_PATH}" >&2
              fi
            '')
            (pkgs.writeShellScriptBin "edit-trading-secrets" ''
              sops secrets.yaml
            '')
          ];
        };
      }) // {
        nixosModules.default = import ./modules/sops.nix;
      };
}