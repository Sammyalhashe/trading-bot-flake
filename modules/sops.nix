{ config, lib, ... }:
{
  sops.defaultSopsFile = ../secrets.yaml;
  sops.age.keyFile = "/var/lib/sops/age/key.txt";
  sops.age.sshKeyPaths = [ "/etc/ssh/ssh_host_ed25519_key" ];
  sops.age.generateKey = true;

  # Trading bot secrets
  sops.secrets.eth_rpc_url = { };
  sops.secrets.eth_private_key = { };
  sops.secrets.telegram_bot_token = { };
  sops.secrets.coinbase_api_id_clawdbot = { };
  sops.secrets.coinbase_api_secret_clawdbot = { };

  # Template for coinbase API JSON file
  sops.templates."coinbase-api-json" = {
    content = ''
      {
         "name": "${config.sops.placeholder.coinbase_api_id_clawdbot}",
         "privateKey": "${config.sops.placeholder.coinbase_api_secret_clawdbot}"
      }
    '';
    path = "/home/%USER%/cdb_api_key.json";  # This will be overridden by the importing configuration
  };

  # Environment file for trading bot (optional)
  sops.templates."trading-bot-env" = {
    content = ''
      ETH_RPC_URL=${config.sops.placeholder.eth_rpc_url}
      ETH_PRIVATE_KEY=${config.sops.placeholder.eth_private_key}
      TELEGRAM_BOT_TOKEN=${config.sops.placeholder.telegram_bot_token}
    '';
  };
}