self:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib)
    mkEnableOption
    mkIf
    mkOption
    optionalAttrs
    types
    ;

  cfg = config.services.trading-bot;

  renderValue = v: if builtins.isBool v then lib.boolToString v else toString v;

  baseEnvironment = [
    "TRADING_MODE=${cfg.mode}"
    "ENABLE_ETHEREUM=${lib.boolToString cfg.ethereum.enable}"
  ]
  ++ lib.mapAttrsToList (name: value: "${name}=${renderValue value}") cfg.settings;

  environmentWith = apiJson: extra: baseEnvironment ++ [ "COINBASE_API_JSON=${apiJson}" ] ++ extra;

  bin = name: "${cfg.package}/bin/${name}";

  waitForCoinbase = "${pkgs.bash}/bin/bash -c 'for i in {1..12}; do if ${pkgs.iputils}/bin/ping -c 1 api.coinbase.com &>/dev/null; then exit 0; fi; sleep 5; done; exit 1'";

  networkOnline = {
    After = [ "network-online.target" ];
    Wants = [ "network-online.target" ];
  };

  apiJsonOption =
    description:
    mkOption {
      type = types.str;
      default = cfg.coinbaseApiJson;
      defaultText = lib.literalExpression "config.services.trading-bot.coinbaseApiJson";
      inherit description;
    };

  dailyTimer = description: onCalendar: {
    Unit.Description = description;
    Timer = {
      OnCalendar = onCalendar;
      Persistent = true;
    };
    Install.WantedBy = [ "timers.target" ];
  };
in
{
  options.services.trading-bot = {
    enable = mkEnableOption "the trading bot user services";

    package = mkOption {
      type = types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalExpression "trading-bot-flake.packages.\${system}.default";
      description = "Package providing trading-bot, trading-report, trading-notify, trading-pnl and collect-derivatives.";
    };

    mode = mkOption {
      type = types.enum [
        "live"
        "paper"
      ];
      default = "paper";
      description = "TRADING_MODE for every service.";
    };

    ethereum.enable = mkEnableOption "the on-chain (Base) executor alongside Coinbase";

    coinbaseApiJson = mkOption {
      type = types.str;
      description = "Path to the Coinbase API key JSON (COINBASE_API_JSON); each service can override it.";
    };

    environmentFiles = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "EnvironmentFile entries for the bot units (secrets such as ETH_PRIVATE_KEY, BASE_RPC_URL).";
    };

    settings = mkOption {
      type = types.attrsOf (
        types.oneOf [
          types.str
          types.int
          types.float
          types.bool
        ]
      );
      default = { };
      example = {
        STRATEGY = "trend_following";
        ATR_STOP_MIN_PCT = 0.06;
        STOPOUT_COOLDOWN_HOURS = 12;
      };
      description = ''
        Extra environment variables for every service. Keys are the bot's own variable
        names from config/trading_config.py and config/executor_config.py.
      '';
    };

    ws = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = "Run the long-lived WebSocket bot (coinbase-trader-ws).";
      };
      coinbaseApiJson = apiJsonOption "COINBASE_API_JSON for coinbase-trader-ws.";
    };

    paper = {
      enable = mkEnableOption "a paper-trading WebSocket bot with a simulated ledger (coinbase-trader-paper)";
      startingCash = mkOption {
        type = types.ints.positive;
        default = 10000;
        description = "Simulated USDC the paper ledger starts with (PAPER_STARTING_CASH).";
      };
      coinbaseApiJson = apiJsonOption "COINBASE_API_JSON for the paper bot (read-only: prices and candles).";
      settings = mkOption {
        type = types.attrsOf (
          types.oneOf [
            types.str
            types.int
            types.float
            types.bool
          ]
        );
        default = { };
        example = {
          STRATEGY = "daily_trend";
          DAILY_TREND_FILTER = "sma:50";
        };
        description = "Environment for the paper bot only; the shared `settings` are not applied to it.";
      };
    };

    timer = {
      autoStart = mkEnableOption "the 3-minute timer fallback (coinbase-trader.timer) at login";
      coinbaseApiJson = apiJsonOption "COINBASE_API_JSON for the timer-mode bot.";
    };

    report.coinbaseApiJson = apiJsonOption "COINBASE_API_JSON for coinbase-report and the notifier.";

    notifier = {
      enable = mkEnableOption "the regime-change Telegram notifier (trading-report-notify)";
      interval = mkOption {
        type = types.str;
        default = "5min";
        description = "How often the notifier runs.";
      };
    };

    pnlReport = {
      enable = mkEnableOption "the daily since-inception PnL report";
      onCalendar = mkOption {
        type = types.str;
        default = "daily";
        description = "When the PnL report runs.";
      };
      snapshotFile = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "PNL_SNAPSHOT_FILE; null keeps the bot's default.";
      };
      coinbaseApiJson = apiJsonOption "COINBASE_API_JSON for the PnL report.";
    };

    costProbe = {
      enable = mkEnableOption "the hourly quote-only Base DEX vs Coinbase swap cost probe";
      onCalendar = mkOption {
        type = types.str;
        default = "hourly";
        description = "When the cost probe runs.";
      };
      sizesUsd = mkOption {
        type = types.str;
        default = "1000,10000";
        description = "PROBE_SIZES_USD: comma-separated round-trip sizes to quote.";
      };
    };

    derivativesCollector = {
      enable = mkEnableOption "the daily OKX derivatives data collector";
      onCalendar = mkOption {
        type = types.str;
        default = "daily";
        description = "When the collector runs.";
      };
      dataDir = mkOption {
        type = types.str;
        default = "${config.xdg.dataHome}/trading-bot/derivatives";
        defaultText = lib.literalExpression ''"''${config.xdg.dataHome}/trading-bot/derivatives"'';
        description = "DERIVATIVES_DATA_DIR the collector appends CSVs to.";
      };
    };
  };

  config = mkIf cfg.enable {
    home.packages =
      lib.optional cfg.ws.enable (
        pkgs.writeShellScriptBin "trading-botctl" ''exec ${bin "trading-botctl"} "$@"''
      )
      ++ lib.optional cfg.costProbe.enable (
        pkgs.writeShellScriptBin "onchain-cost-report" ''exec ${bin "onchain-cost-report"} "$@"''
      )
      ++ lib.optional cfg.paper.enable (
        pkgs.writeShellScriptBin "trading-botctl-paper" ''
          TRADING_BOT_SOCKET="$XDG_RUNTIME_DIR/trading-bot-paper.sock" exec ${bin "trading-botctl"} "$@"
        ''
      );

    systemd.user.services = {
      coinbase-trader-ws = mkIf cfg.ws.enable {
        Unit = networkOnline // {
          Description = "Coinbase Trading Bot (WebSocket mode)";
          Conflicts = [
            "coinbase-trader.service"
            "coinbase-trader.timer"
          ];
        };
        Service = {
          Type = "simple";
          Restart = "on-failure";
          RestartSec = "10s";
          Environment = environmentWith cfg.ws.coinbaseApiJson [ ];
          EnvironmentFile = cfg.environmentFiles;
          ExecStartPre = waitForCoinbase;
          ExecStart = "${bin "trading-bot"} --ws";
        };
        Install.WantedBy = [ "default.target" ];
      };

      coinbase-trader-paper = mkIf cfg.paper.enable {
        Unit = networkOnline // {
          Description = "Coinbase Trading Bot (paper ledger)";
        };
        Service = {
          Type = "simple";
          Restart = "on-failure";
          RestartSec = "10s";
          # Own state, log and socket so it never shares files or the run lock with the live bot.
          Environment = [
            "TRADING_MODE=paper"
            "ENABLE_ETHEREUM=false"
            "COINBASE_API_JSON=${cfg.paper.coinbaseApiJson}"
            "PAPER_STARTING_CASH=${toString cfg.paper.startingCash}"
            "TRADING_STATE_FILE=%h/.local/state/trading-bot-paper/trading_state.json"
            "TRADING_LOG_FILE=%h/.local/state/trading-bot-paper/trading.log"
            "TRADING_BOT_SOCKET=%t/trading-bot-paper.sock"
            "CONTROL_TELEGRAM=false"
            "NOTIFY_PREFIX=PAPER"
          ]
          ++ lib.mapAttrsToList (name: value: "${name}=${renderValue value}") cfg.paper.settings;
          ExecStartPre = waitForCoinbase;
          ExecStart = "${bin "trading-bot"} --ws";
        };
        Install.WantedBy = [ "default.target" ];
      };

      coinbase-trader = {
        Unit = networkOnline // {
          Description = "Run Coinbase Trading Bot";
        };
        Service = {
          Type = "oneshot";
          Environment = environmentWith cfg.timer.coinbaseApiJson [ ];
          EnvironmentFile = cfg.environmentFiles;
          ExecStartPre = waitForCoinbase;
          ExecStart = bin "trading-bot";
        };
      };

      coinbase-report = {
        Unit.Description = "Run Coinbase Trading Report";
        Service = {
          Type = "oneshot";
          Environment = environmentWith cfg.report.coinbaseApiJson [ ];
          ExecStart = bin "trading-report";
        };
      };

      trading-report-notify = mkIf cfg.notifier.enable {
        Unit.Description = "Run Trading Report and Notify Telegram";
        Service = {
          Type = "oneshot";
          Environment = environmentWith cfg.report.coinbaseApiJson [ ];
          ExecStart = [
            (bin "trading-report")
            (bin "trading-notify")
          ];
        };
      };

      coinbase-pnl-report = mkIf cfg.pnlReport.enable {
        Unit = networkOnline // {
          Description = "Run Coinbase Trading Bot PnL Report";
        };
        Service = {
          Type = "oneshot";
          Environment = environmentWith cfg.pnlReport.coinbaseApiJson (
            lib.optional (cfg.pnlReport.snapshotFile != null) "PNL_SNAPSHOT_FILE=${cfg.pnlReport.snapshotFile}"
          );
          ExecStart = bin "trading-pnl";
        };
      };

      trading-bot-cost-probe = mkIf cfg.costProbe.enable {
        Unit = networkOnline // {
          Description = "Quote Base DEX and Coinbase round-trip swap costs (no orders)";
        };
        Service = {
          Type = "oneshot";
          Environment = [ "PROBE_SIZES_USD=${cfg.costProbe.sizesUsd}" ];
          EnvironmentFile = cfg.environmentFiles;
          ExecStart = bin "onchain-cost-probe";
        };
      };

      derivatives-collector = mkIf cfg.derivativesCollector.enable {
        Unit = networkOnline // {
          Description = "Collect OKX derivatives data for backtesting";
        };
        Service = {
          Type = "oneshot";
          Environment = baseEnvironment ++ [ "DERIVATIVES_DATA_DIR=${cfg.derivativesCollector.dataDir}" ];
          ExecStart = bin "collect-derivatives";
        };
      };
    };

    systemd.user.timers = {
      coinbase-trader = {
        Unit.Description = "Run Coinbase Trading Bot every 3 minutes";
        Timer = {
          OnCalendar = "*:0/3";
          Persistent = true;
        };
      }
      // optionalAttrs cfg.timer.autoStart { Install.WantedBy = [ "timers.target" ]; };

      trading-report-notify = mkIf cfg.notifier.enable {
        Unit.Description = "Run Trading Report and Notify every ${cfg.notifier.interval}";
        Timer = {
          OnBootSec = cfg.notifier.interval;
          OnUnitActiveSec = cfg.notifier.interval;
          AccuracySec = "1s";
        };
        Install.WantedBy = [ "timers.target" ];
      };

      coinbase-pnl-report = mkIf cfg.pnlReport.enable (
        dailyTimer "Run Coinbase Trading Bot PnL Report" cfg.pnlReport.onCalendar
      );

      trading-bot-cost-probe = mkIf cfg.costProbe.enable (
        dailyTimer "Hourly Base DEX vs Coinbase swap cost probe" cfg.costProbe.onCalendar
      );

      derivatives-collector = mkIf cfg.derivativesCollector.enable (
        dailyTimer "Daily OKX derivatives data collection" cfg.derivativesCollector.onCalendar
      );
    };
  };
}
