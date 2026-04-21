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
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];

    xdg.configFile."pipewire/pipewire.conf.d/20-transcribe-meeting-echo-cancel.conf".text = ''
      context.modules = [
          {   name = libpipewire-module-echo-cancel
              args = {
                  library.name  = aec/libspa-aec-webrtc
                  node.latency  = 1024/48000
                  source.props = {
                      node.name        = "echo-cancel-source"
                      node.description = "Echo-cancelled microphone"
                  }
                  sink.props = {
                      node.name        = "echo-cancel-sink"
                      node.description = "Echo-cancel reference sink"
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
