self:
{ config, lib, pkgs, ... }:
let
  cfg = config.programs.page-archiver;
  json = pkgs.formats.json { };
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
      description = "Public configuration for config.json. Keep credentials out of the Nix store; use the environment or credential_command seam.";
    };
  };
  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];
    xdg.configFile."page-archiver/config.json".source = json.generate "page-archiver-config.json" cfg.settings;
  };
}
