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

      derivatives-collector = mkIf cfg.derivativesCollector.enable (
        dailyTimer "Daily OKX derivatives data collection" cfg.derivativesCollector.onCalendar
      );
    };
  };
}
