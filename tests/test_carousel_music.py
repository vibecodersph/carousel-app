from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import carousel_music


class CarouselMusicTests(unittest.TestCase):
    def test_select_requested_track_resolves_relative_path_and_caps_duration(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            (clips / "signal.mp3").write_bytes(b"audio")
            library = root / "library.json"
            library.write_text(
                json.dumps(
                    {
                        "tracks": [
                            {
                                "id": "signal-glow",
                                "title": "Signal Glow",
                                "path": "clips/signal.mp3",
                                "duration_seconds": 75,
                                "volume": 0.31,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            track = carousel_music.select_music_track(
                library_path=library,
                carousel_id="brief-router",
                requested_clip_id="signal-glow",
            )

        self.assertIsNotNone(track)
        assert track is not None
        self.assertEqual(track.clip_id, "signal-glow")
        self.assertEqual(track.path.name, "signal.mp3")
        self.assertEqual(track.duration_seconds, carousel_music.MAX_SHORT_CLIP_SECONDS)
        self.assertEqual(track.volume, 0.31)

    def test_missing_library_returns_no_track(self) -> None:
        with TemporaryDirectory() as tmp:
            track = carousel_music.select_music_track(
                library_path=Path(tmp) / "missing.json",
                carousel_id="brief-router",
            )

        self.assertIsNone(track)

    def test_add_music_to_video_builds_short_faded_ffmpeg_command(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "cover.mp4"
            audio = root / "clip.wav"
            out = root / "cover_music.mp4"
            video.write_bytes(b"video")
            audio.write_bytes(b"audio")
            track = carousel_music.MusicTrack(
                clip_id="clip",
                path=audio,
                start_seconds=2.0,
                duration_seconds=24.0,
                volume=0.5,
                fade_seconds=1.0,
            )
            commands: list[list[str]] = []

            with patch.object(carousel_music.shutil, "which", return_value="/usr/bin/ffmpeg"):
                carousel_music.add_music_to_video(
                    video,
                    track,
                    out,
                    duration_seconds=18.5,
                    run_command=commands.append,
                )

        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertEqual(command[0], "/usr/bin/ffmpeg")
        self.assertIn("-stream_loop", command)
        self.assertIn("-ss", command)
        self.assertIn("2.000", command)
        self.assertIn("-t", command)
        self.assertIn("18.500", command)
        self.assertIn("-af", command)
        filter_arg = command[command.index("-af") + 1]
        self.assertIn("volume=0.500", filter_arg)
        self.assertIn("afade=t=out:st=17.500:d=1.000", filter_arg)
        self.assertEqual(command[-1], str(out))


if __name__ == "__main__":
    unittest.main()
