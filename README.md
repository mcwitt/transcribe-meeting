# transcribe-meeting

Stream microphone + system-output audio to [Deepgram](https://deepgram.com) `nova-3` and print a diarized live transcript to stdout. Linux / PipeWire only.

Output looks like:

```
[me]        Hey, can you hear me okay?
[remote-0]  Yeah, you're coming through clear.
[remote-1]  Me too. Should we get started?
```

`[me]` is you (mic channel). `[remote-N]` is a participant on the far side, diarized by Deepgram within the system-audio channel.

## Requirements

- PipeWire (`pw-record`, `pw-metadata`, `pw-dump`).
- A Deepgram API key in `$DEEPGRAM_API_KEY`.
- Nix with flakes, or Python ≥3.11 with `numpy` and `websockets`.
- For combo webcams whose mic only flows while video is active (e.g. OBSBOT): `v4l-utils`.

## Quickstart (Nix flake)

```sh
nix run github:mcwitt/transcribe-meeting -- --help
```

Or clone and develop:

```sh
git clone https://github.com/mcwitt/transcribe-meeting
cd transcribe-meeting
cp .envrc.example .envrc
# add your DEEPGRAM_API_KEY to .envrc
direnv allow   # or: nix develop
transcribe-meeting
```

## Usage

```
transcribe-meeting [--mic NAME] [--system SINK] [--keep-camera DEVICE] [--list-sources]
```

- `--mic` — PipeWire node name for the mic. Defaults to the current default source.
- `--system` — PipeWire sink whose monitor to capture. Defaults to the current default sink.
- `--keep-camera /dev/videoN` — hold a V4L2 video stream open for the duration of the run. Needed for combo webcams (OBSBOT Meet, etc.) whose USB audio only streams while the video pipeline is active.
- `--list-sources` — dump PipeWire audio sources and sinks and exit.

## Echo / feedback

If you don't wear headphones, the remote's speech reaches the mic through your speakers and gets double-transcribed as both `[me]` and `[remote-N]`. Two fixes:

### PipeWire's echo canceller (recommended)

Drop a config fragment that loads `libpipewire-module-echo-cancel` (WebRTC AEC3), then route playback through `echo-cancel-sink` and capture `echo-cancel-source`. See [`module.nix`](./module.nix) for a ready-made config.

### Home-manager module

The flake exposes `homeManagerModules.default` which installs the CLI and drops the echo-cancel config:

```nix
{
  inputs.transcribe-meeting.url = "github:mcwitt/transcribe-meeting";

  # in your home-manager config:
  imports = [ inputs.transcribe-meeting.homeManagerModules.default ];
  programs.transcribe-meeting.enable = true;
}
```

After the first activation, restart the session's PipeWire:

```sh
systemctl --user restart pipewire wireplumber
```

Then set `echo-cancel-sink` as your default sink (`wpctl set-default <id>` or via your audio UI) and pass `--mic echo-cancel-source` to `transcribe-meeting`.

## How it works

- Two `pw-record` subprocesses capture the mic (mono) and the default sink's monitor (mono, via `stream.capture.sink=true`).
- Chunks are shipped into bounded `asyncio.Queue`s (drop-oldest on overflow).
- A drift-corrected 100 ms ticker interleaves one mic + one system chunk into stereo int16 and sends it to Deepgram over WebSocket with `multichannel=true&diarize=true`.
- Deepgram returns a channel index (0 = mic = you, 1 = system = remotes) and a per-word speaker index inside the system channel, which the CLI groups by consecutive speaker and prints one line at a time.

## License

MIT.
