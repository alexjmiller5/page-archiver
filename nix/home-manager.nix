self:
{ config, lib, pkgs, ... }:
let
  cfg = config.programs.page-archiver;
  json = pkgs.formats.json { };
  executable = lib.getExe cfg.package;
  environment = {
    XDG_CONFIG_HOME = config.xdg.configHome;
    HOME = config.home.homeDirectory;
  } // cfg.service.environment // {
    # Include configuration in the service definition so activation reloads the
    # long-running process even when its executable and environment are unchanged.
    PAGE_ARCHIVER_SETTINGS_HASH = builtins.hashString "sha256" (builtins.toJSON cfg.settings);
  };
in
{
  options.programs.page-archiver = {
    enable = lib.mkEnableOption "Page Archiver";
    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      description = "The installed Page Archiver package.";
    };
    settings = lib.mkOption {
      type = json.type;
      default = { };
      description = "Public config.json values. Use credential_command for secure credential access; never put credentials in the Nix store.";
    };
    service = {
      enable = lib.mkEnableOption "the outbound Page Archiver background runner";
      environment = lib.mkOption {
        type = lib.types.attrsOf lib.types.str;
        default = { };
        description = "Nonsecret environment values for the runner and its credential command. This environment is independent of the interactive shell.";
      };
    };
  };
  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      assertions = [{
        assertion = !(cfg.settings ? hub_token) && !(cfg.service.environment ? PAGE_ARCHIVER_HUB_TOKEN);
        message = "Page Archiver credentials must stay out of Nix settings/environment; use a secure credential_command.";
      }];
      home.packages = [ cfg.package ];
      xdg.configFile."page-archiver/config.json".source = json.generate "page-archiver-config.json" cfg.settings;
    }
    (lib.mkIf (cfg.service.enable && pkgs.stdenv.hostPlatform.isDarwin) {
      launchd.agents.page-archiver = {
        enable = true;
        config = {
          ProgramArguments = [ executable "watch" ];
          EnvironmentVariables = environment;
          RunAtLoad = true;
          # Retry network/cap failures inside the process. A deliberate fatal
          # exit stays stopped until the credential/configuration is repaired.
          KeepAlive = { SuccessfulExit = true; Crashed = true; };
          ThrottleInterval = 30;
          ProcessType = "Background";
          StandardOutPath = "${config.home.homeDirectory}/Library/Logs/page-archiver.log";
          StandardErrorPath = "${config.home.homeDirectory}/Library/Logs/page-archiver.log";
          Umask = 63;
        };
      };
    })
    (lib.mkIf (cfg.service.enable && pkgs.stdenv.hostPlatform.isLinux) {
      systemd.user.services.page-archiver = {
        Unit.Description = "Retain web pages from a durable outbound subscription";
        Install.WantedBy = [ "default.target" ];
        Service = {
          ExecStart = "${executable} watch";
          Environment = lib.mapAttrsToList (name: value: "${name}=${value}") environment;
          Restart = "on-failure";
          RestartPreventExitStatus = [ 78 ];
          RestartSec = 30;
          UMask = "0077";
        };
      };
    })
  ]);
}
