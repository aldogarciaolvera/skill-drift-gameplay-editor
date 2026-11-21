"""Automated unit and integration tests for drift_gameplay_cli.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import drift_gameplay_cli as cli  # noqa: E402


class _MockMcpHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        auth = self.headers.get("Authorization", "")
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if auth != "Bearer secret-tok":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"unauthorized")
            return
        tool = body["params"]["name"]
        if tool == "fail_tool":
            payload = {"ok": False, "error": "bad_args", "detail": "invalid test arg"}
        else:
            payload = {"ok": True, "tool": tool, "echo": body["params"]["arguments"]}
        rpc = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}
        raw = json.dumps(rpc).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class DriftGameplayCliTests(unittest.TestCase):
    def test_mcp_success_and_error(self) -> None:
        srv = HTTPServer(("127.0.0.1", 0), _MockMcpHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                out_ok = Path(td) / "ok.json"
                rc = cli.main(
                    [
                        "mcp",
                        "--url",
                        f"http://127.0.0.1:{port}/mcp",
                        "--token",
                        "secret-tok",
                        "--tool",
                        "inspect",
                        "--args-json",
                        '{"clips": true}',
                        "--output",
                        str(out_ok),
                    ]
                )
                self.assertEqual(rc, 0)
                d_ok = json.loads(out_ok.read_text(encoding="utf-8"))
                self.assertTrue(d_ok["ok"])
                self.assertEqual(d_ok["echo"], {"clips": True})

                out_err = Path(td) / "err.json"
                rc_err = cli.main(
                    [
                        "mcp",
                        "--url",
                        f"http://127.0.0.1:{port}/mcp",
                        "--token",
                        "secret-tok",
                        "--tool",
                        "fail_tool",
                        "--output",
                        str(out_err),
                    ]
                )
                self.assertEqual(rc_err, 1)
                d_err = json.loads(out_err.read_text(encoding="utf-8"))
                self.assertFalse(d_err["ok"])
                self.assertEqual(d_err["error"], "bad_args")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_classify_music_preserves_dialogue(self) -> None:
        # 20s music from 4..24s with spoken dialogue (non-singing) from 10..13s -> 2 mute segments
        times = [round(i * 0.48, 2) for i in range(60)]
        mu = [0.8 if 4.0 <= t <= 24.0 else 0.05 for t in times]
        sp = [0.9 if 10.0 <= t <= 13.0 else 0.05 for t in times]
        si = [0.05 if 10.0 <= t <= 13.0 else (0.4 if 4.0 <= t <= 24.0 else 0.0) for t in times]
        res = cli.classify_music_segments(times, sp, mu, si)
        mutes = res["proposed_mute_windows"]
        self.assertEqual(len(mutes), 2)
        self.assertLess(mutes[0]["end"], 10.0)
        self.assertGreater(mutes[1]["start"], 13.0)

    def test_plan_youtube_two_stage_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            insp_pre = {
                "tracks": [
                    {
                        "i": 0,
                        "type": "video",
                        "label": "Video 1",
                        "items": [
                            {
                                "id": "vid-1",
                                "assetId": "asset-1",
                                "name": "1.mp4",
                                "path": "C:/v/1.mp4",
                                "start": 0.0,
                                "duration": 120.0,
                                "inPoint": 0.0,
                                "outPoint": 120.0,
                                "speed": 1.0,
                                "linkId": "lnk-1",
                            }
                        ],
                    },
                    {
                        "i": 1,
                        "type": "audio",
                        "label": "Audio 1",
                        "items": [
                            {
                                "id": "aud-1",
                                "assetId": "asset-1",
                                "name": "1.mp4",
                                "path": "C:/v/1.mp4",
                                "start": 0.0,
                                "duration": 120.0,
                                "inPoint": 0.0,
                                "outPoint": 120.0,
                                "speed": 1.0,
                                "linkId": "lnk-1",
                            }
                        ],
                    },
                ]
            }
            pauses = {
                "video": {"path": "C:/v/1.mp4"},
                "suggested_cuts": [
                    {"start": 0.0, "end": 10.0, "duration": 10.0, "kind": "pause", "transition": False},
                    {"start": 40.0, "end": 46.0, "duration": 6.0, "kind": "pause", "transition": True},
                ],
                "review_candidates": [{"start": 70.0, "end": 72.0, "duration": 2.0, "kind": "short_map", "reason": "short map"}],
            }
            music = {
                "source_path": "C:/v/1.mp4",
                "proposed_mute_windows": [{"start": 20.0, "end": 30.0, "duration": 10.0}],
                "review_windows": [{"start": 90.0, "end": 95.0, "duration": 5.0, "reason": "ambiguous synth"}],
            }
            fill_assets = {
                "tracks": [
                    {
                        "id": "ftu-nd",
                        "title": "Night Drive",
                        "artist": "Walen",
                        "attribution": "Night Drive by Walen (freetouse.com)",
                        "license_url": "https://freetouse.com/license",
                        "dur": 137.5,
                        "volume": 0.32,
                        "mood": "driving",
                    }
                ],
                "whoosh_sfx": {"id": "sfx-whoosh", "dur": 0.6},
            }

            (tdp / "insp_pre.json").write_text(json.dumps(insp_pre), encoding="utf-8")
            (tdp / "pauses.json").write_text(json.dumps(pauses), encoding="utf-8")
            (tdp / "music.json").write_text(json.dumps(music), encoding="utf-8")
            (tdp / "fill.json").write_text(json.dumps(fill_assets), encoding="utf-8")

            out_dir = tdp / "plan"
            rc1 = cli.main(
                [
                    "plan-youtube",
                    "--stage",
                    "cuts",
                    "--inspect-file",
                    str(tdp / "insp_pre.json"),
                    "--pauses-file",
                    str(tdp / "pauses.json"),
                    "--out-dir",
                    str(out_dir),
                ]
            )
            self.assertEqual(rc1, 0)
            b1a = json.loads((out_dir / "batch_1a_trims_splits.json").read_text(encoding="utf-8"))
            self.assertEqual(b1a["ops"][0]["tool"], "set_trim")
            self.assertEqual(b1a["ops"][0]["args"]["in"], 10.0)
            splits = [op["args"]["at"] for op in b1a["ops"] if op["tool"] == "split_clip"]
            self.assertEqual(splits, [36.0, 30.0])  # descending relative to trimmed in=10

            # Simulate post-cut timeline (10..40 and 46..120)
            insp_post = {
                "tracks": [
                    {
                        "i": 0,
                        "type": "video",
                        "label": "Video 1",
                        "items": [
                            {
                                "id": "vid-1a",
                                "assetId": "asset-1",
                                "name": "1.mp4",
                                "path": "C:/v/1.mp4",
                                "start": 0.0,
                                "duration": 30.0,
                                "inPoint": 10.0,
                                "outPoint": 40.0,
                                "speed": 1.0,
                                "linkId": "lnk-1a",
                            },
                            {
                                "id": "vid-1b",
                                "assetId": "asset-1",
                                "name": "1.mp4",
                                "path": "C:/v/1.mp4",
                                "start": 30.0,
                                "duration": 74.0,
                                "inPoint": 46.0,
                                "outPoint": 120.0,
                                "speed": 1.0,
                                "linkId": "lnk-1b",
                            },
                        ],
                    },
                    {
                        "i": 1,
                        "type": "audio",
                        "label": "Audio 1",
                        "items": [
                            {
                                "id": "aud-1a",
                                "assetId": "asset-1",
                                "name": "1.mp4",
                                "path": "C:/v/1.mp4",
                                "start": 0.0,
                                "duration": 30.0,
                                "inPoint": 10.0,
                                "outPoint": 40.0,
                                "speed": 1.0,
                                "linkId": "lnk-1a",
                            },
                            {
                                "id": "aud-1b",
                                "assetId": "asset-1",
                                "name": "1.mp4",
                                "path": "C:/v/1.mp4",
                                "start": 30.0,
                                "duration": 74.0,
                                "inPoint": 46.0,
                                "outPoint": 120.0,
                                "speed": 1.0,
                                "linkId": "lnk-1b",
                            },
                        ],
                    },
                ]
            }
            (tdp / "insp_post.json").write_text(json.dumps(insp_post), encoding="utf-8")

            rc2 = cli.main(
                [
                    "plan-youtube",
                    "--stage",
                    "post-cuts",
                    "--inspect-file",
                    str(tdp / "insp_post.json"),
                    "--pauses-file",
                    str(tdp / "pauses.json"),
                    "--music-file",
                    str(tdp / "music.json"),
                    "--fill-assets-file",
                    str(tdp / "fill.json"),
                    "--out-dir",
                    str(out_dir),
                ]
            )
            self.assertEqual(rc2, 0)
            b2 = json.loads((out_dir / "batch_2_transitions_mutes.json").read_text(encoding="utf-8"))
            tools_b2 = [op["tool"] for op in b2["ops"]]
            self.assertIn("add_transition", tools_b2)
            self.assertIn("set_keyframes", tools_b2)
            self.assertIn("add_bookmark", tools_b2)
            kf_op = next(op for op in b2["ops"] if op["tool"] == "set_keyframes")
            times = [pt[0] for pt in kf_op["args"]["keys"]["volume"]]
            self.assertEqual(times, sorted(times))
            self.assertEqual(len(times), len(set(times)))
            credits_txt = (out_dir / "youtube_credits.txt").read_text(encoding="utf-8")
            self.assertIn("Night Drive by Walen", credits_txt)
            self.assertTrue((out_dir / "chapters.txt").exists())
            self.assertTrue((out_dir / "edit_report.md").exists())

    def test_find_highlights_and_build_reel_subs(self) -> None:
        # Build synthetic 400s transcript with 6 exciting windows
        segments = []
        for idx, t0 in enumerate([10, 80, 155, 230, 305, 360]):
            for off in range(0, 50, 5):
                sa = float(t0 + off)
                sb = sa + 3.2
                txt = "OMG what a clutch! Let's go!" if idx % 2 == 0 else "Wait no way! Watch out!"
                words = []
                toks = txt.split()
                for wi, tok in enumerate(toks):
                    wa = sa + wi * 0.45
                    words.append({"start": round(wa, 2), "end": round(wa + 0.4, 2), "word": tok, "prob": 0.95})
                segments.append(
                    {
                        "tag": "FULL",
                        "start": sa,
                        "end": sb,
                        "text": txt,
                        "no_speech_prob": 0.05,
                        "avg_logprob": -0.2,
                        "words": words,
                    }
                )
        transcript = {"ok": True, "duration_sec": 430.0, "segments": segments}

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            tpath = tdp / "full_tr.json"
            hpath = tdp / "highlights.json"
            tpath.write_text(json.dumps(transcript), encoding="utf-8")

            rc_h = cli.main(
                [
                    "find-highlights",
                    "--transcript-file",
                    str(tpath),
                    "--min-dur",
                    "60",
                    "--max-dur",
                    "75",
                    "--min-count",
                    "5",
                    "--output",
                    str(hpath),
                ]
            )
            self.assertEqual(rc_h, 0)
            h_doc = json.loads(hpath.read_text(encoding="utf-8"))
            self.assertGreaterEqual(h_doc["count"], 5)
            for h in h_doc["highlights"]:
                self.assertGreaterEqual(h["duration"], 60.0)

            subs_dir = tdp / "subs"
            rc_s1 = cli.main(
                [
                    "build-reel-subs",
                    "--transcript-file",
                    str(tpath),
                    "--ranges-file",
                    str(hpath),
                    "--words-per-cue",
                    "3",
                    "--out-dir",
                    str(subs_dir),
                ]
            )
            self.assertEqual(rc_s1, 0)
            cues_tr = json.loads((subs_dir / "cues_for_translation.json").read_text(encoding="utf-8"))
            self.assertGreater(len(cues_tr), 5)

            translated = [{"index": c["index"], "text_es": f"ES: {c['text_en']}"} for c in cues_tr]
            tr_es_path = tdp / "translated.json"
            tr_es_path.write_text(json.dumps(translated), encoding="utf-8")

            rc_s2 = cli.main(
                [
                    "build-reel-subs",
                    "--transcript-file",
                    str(tpath),
                    "--ranges-file",
                    str(hpath),
                    "--translated-json",
                    str(tr_es_path),
                    "--words-per-cue",
                    "3",
                    "--out-dir",
                    str(subs_dir),
                ]
            )
            self.assertEqual(rc_s2, 0)
            en_srt = (subs_dir / "reels_en.srt").read_text(encoding="utf-8")
            es_srt = (subs_dir / "reels_es.srt").read_text(encoding="utf-8")
            self.assertIn("-->", en_srt)
            self.assertIn("ES:", es_srt)

    def test_scan_pauses_with_synthetic_ffmpeg_video(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            vid = tdp / "test_vid.mp4"
            ref = tdp / "ref_pause.png"
            out = tdp / "scan_out.json"
            # Generate 9s video: 0..3s noise, 3..6s solid red/box pause screen, 6..9s noise
            fc = (
                "color=c=blue:s=320x180:d=3[a];"
                "color=c=darkred:s=320x180:d=3,drawbox=x=20:y=20:w=80:h=60:color=white:t=fill[b];"
                "color=c=green:s=320x180:d=3[c];"
                "[a][b][c]concat=n=3:v=1:a=0"
            )
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-filter_complex", fc, "-r", "20", str(vid)],
                check=True,
            )
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-ss", "4.0", "-i", str(vid), "-frames:v", "1", str(ref)],
                check=True,
            )
            rc = cli.main(
                [
                    "scan-pauses",
                    "--video",
                    str(vid),
                    "--ref-pause",
                    str(ref),
                    "--min-pause-sec",
                    "2.0",
                    "--output",
                    str(out),
                ]
            )
            self.assertEqual(rc, 0)
            doc = json.loads(out.read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(doc["pause_segments"]), 1)
            p0 = doc["pause_segments"][0]
            self.assertAlmostEqual(p0["start"], 3.0, delta=0.4)
            self.assertAlmostEqual(p0["end"], 6.0, delta=0.4)


if __name__ == "__main__":
    unittest.main()
