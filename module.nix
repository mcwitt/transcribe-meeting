{ self }:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.programs.transcribe-meeting;
in
{
  options.programs.transcribe-meeting = {
    enable = lib.mkEnableOption ''
      the transcribe-meeting CLI and a PipeWire echo-cancel virtual mic/sink pair.

      Drops a config at ~/.config/pipewire/pipewire.conf.d/ that loads
      libpipewire-module-echo-cancel (WebRTC AEC3). This creates two virtual
      nodes:

        echo-cancel-source  — mic with speaker output subtracted
        echo-cancel-sink    — anything routed here is the cancellation reference

      For cancellation to engage, playback must go through echo-cancel-sink
      (set it as the default sink, or route the meeting app there manually).
      Then pass `--mic echo-cancel-source` to transcribe-meeting, or make it
      the default source via wpctl/wireplumber.

      Changes take effect after `systemctl --user restart pipewire wireplumber`.
    '';

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalExpression ''
        transcribe-meeting.packages.''${pkgs.stdenv.hostPlatform.system}.default
      '';
      description = "The transcribe-meeting package to install.";
    };

    envFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/run/agenix/deepgram-api-key";
      description = ''
        Path to a file containing environment variable assignments
        (e.g. `DEEPGRAM_API_KEY=...`) to source before running the CLI.
        Intended for secret managers like agenix/sops that decrypt to a
        runtime path. Both `KEY=VAL` and `export KEY=VAL` formats work.

        When set, the installed `transcribe-meeting` is a wrapper that
        sources this file on every invocation.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [
      (
        if cfg.envFile == null then
          cfg.package
        else
          pkgs.symlinkJoin {
            name = "${cfg.package.name}-env-wrapped";
            paths = [ cfg.package ];
            nativeBuildInputs = [ pkgs.makeWrapper ];
            postBuild = ''
              wrapProgram $out/bin/transcribe-meeting \
                --run 'set -a; . "${cfg.envFile}"; set +a'
            '';
          }
      )
    ];

    xdg.configFile."pipewire/pipewire.conf.d/20-transcribe-meeting-echo-cancel.conf".text = ''
      # monitor.mode=true makes the module use the current default sink's
      # monitor as the echo reference, so apps can keep playing to the real
      # hardware sink — no re-routing required. The module exposes a single
      # virtual source, "echo-cancel-source", which is the real mic with the
      # speaker-to-mic echo path subtracted by WebRTC AEC3.
      context.modules = [
          {   name = libpipewire-module-echo-cancel
              args = {
                  library.name  = aec/libspa-aec-webrtc
                  monitor.mode  = true
                  node.latency  = 1024/48000
                  source.props = {
                      node.name        = "echo-cancel-source"
                      node.description = "Echo-cancelled microphone"
                  }
                  aec.args = {
                      webrtc.gain_control       = true
                      webrtc.extended_filter    = true
                      webrtc.delay_agnostic     = true
                      webrtc.noise_suppression  = true
                      webrtc.voice_detection    = true
                  }
              }
          }
      ]
    '';
  };
}
