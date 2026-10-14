"""Headless tests only; visual Qt acceptance is performed by the user."""
import os
import asyncio
import random
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

import chess
import chess.engine

from ai_chess.game import Game, Settings
from ai_chess.logs import export_own_games, read_games
from ai_chess.uci import Engine

ROOT = Path(__file__).resolve().parents[1]
ENGINE = Path(os.environ.get("CHESS_ENGINE", ROOT / "build/release/ai_chess_engine"))
BOOK = ENGINE.parent / "chess_book"


def inspect(board, moves=()):
    result = subprocess.run([str(ENGINE), "--inspect", board.fen(en_passant="fen"), *moves], capture_output=True, text=True, check=True)
    fen, key, draw, legal = result.stdout.splitlines()
    return fen, int(key), bool(int(draw)), set(legal.split())


class RulesTests(unittest.TestCase):
    def test_differential_moves(self):
        rng = random.Random(731)
        boards = [chess.Board(fen) for fen in [
            chess.STARTING_FEN,
            "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1",
            "k3r3/8/8/3pP3/8/8/8/4K3 w - d6 0 1",
            "7k/P7/8/8/8/8/7p/K7 w - - 0 1",
            "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",
        ]]
        for _ in range(8):
            board = chess.Board()
            for _ in range(65):
                boards.append(board.copy(stack=True))
                moves = list(board.legal_moves)
                if not moves:
                    break
                board.push(rng.choice(moves))
        for board in boards:
            with self.subTest(fen=board.fen()):
                fen, _, draw, legal = inspect(board)
                self.assertEqual(fen, board.fen(en_passant="fen"))
                self.assertEqual(legal, {move.uci() for move in board.legal_moves})
                self.assertEqual(draw, board.halfmove_clock >= 100 or board.is_insufficient_material())

    def test_every_special_move_restores_correct_position(self):
        for fen in [
            "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",
            "7k/P7/8/8/8/8/7p/K7 w - - 0 1",
            "7k/8/8/3pP3/8/8/8/K7 w - d6 0 1",
        ]:
            board = chess.Board(fen)
            for move in board.legal_moves:
                after = board.copy()
                after.push(move)
                actual, _, _, legal = inspect(board, [move.uci()])
                self.assertEqual(actual, after.fen(en_passant="fen"))
                self.assertEqual(legal, {m.uci() for m in after.legal_moves})

    def test_repetition_and_pinned_ep_key(self):
        cycle = ["g1f3", "g8f6", "f3g1", "f6g8"] * 2
        self.assertTrue(inspect(chess.Board(), cycle)[2])
        pinned = chess.Board("k3r3/8/8/3pP3/8/8/8/4K3 w - d6 0 1")
        plain = pinned.copy()
        plain.ep_square = None
        self.assertEqual(inspect(pinned)[1], inspect(plain)[1])


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.settings = Settings(str(ENGINE), str(ENGINE))
        self.engine = Engine(str(ENGINE), self.settings, Path(self.directory.name) / "stderr.log", "test")

    def tearDown(self):
        self.engine.close()
        self.engine.wait_closed()
        self.directory.cleanup()

    def test_search_stop_and_deadline(self):
        board = chess.Board()
        start = time.monotonic()
        result = self.engine.play(board, Settings(None, None, 0.05, 20), "test")
        self.assertIn(result.move, board.legal_moves)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertGreaterEqual(result.info["depth"], 1)
        self.engine.send("position startpos")
        self.engine.send("go infinite")
        deadline = time.monotonic() + 3
        while not self.engine.receive(deadline).startswith("info depth "):
            pass
        self.engine.ready()
        start = time.monotonic()
        self.engine.stop()
        while not (line := self.engine.receive(deadline)).startswith("bestmove "):
            pass
        self.assertIn(chess.Move.from_uci(line.split()[1]), board.legal_moves)
        self.assertLess(time.monotonic() - start, 1)

    def test_mate_and_terminal(self):
        board = chess.Board("7k/8/5KQ1/8/8/8/8/8 w - - 0 1")
        result = self.engine.play(board, Settings(None, None, None, 3), "test")
        board.push(result.move)
        self.assertTrue(board.is_checkmate())
        result = self.engine.play(board, Settings(None, None, None, 2), "test")
        self.assertIsNone(result.move)

    def test_elementary_endgames(self):
        for fen in ["7k/8/8/8/8/5K2/8/R7 w - - 0 1", "7k/8/8/8/8/5K2/Q7/8 w - - 0 1", "k7/4P3/4K3/8/8/8/8/8 w - - 0 1"]:
            board = chess.Board(fen)
            for _ in range(100):
                if board.is_game_over(claim_draw=True):
                    break
                result = self.engine.play(board, Settings(None, None, None, 4), "test")
                self.assertIn(result.move, board.legal_moves)
                board.push(result.move)
            self.assertTrue(board.is_checkmate(), board.fen())

    def test_python_chess_async_compatibility(self):
        async def check():
            # Same-thread asyncio works with ordinary subprocess pipes. A
            # heartbeat also lets the loop reap children when the sandbox
            # blocks the child watcher's socket-based wakeup.
            async def heartbeat():
                while True:
                    await asyncio.sleep(0.01)
            ticker = asyncio.create_task(heartbeat())
            transport = None
            try:
                transport, protocol = await asyncio.wait_for(chess.engine.popen_uci(str(ENGINE)), 3)
                board = chess.Board()
                result = await asyncio.wait_for(protocol.play(board, chess.engine.Limit(time=0.02, depth=3)), 3)
                self.assertIn(result.move, board.legal_moves)
                await asyncio.wait_for(protocol.quit(), 3)
            finally:
                if transport:
                    transport.close()
                ticker.cancel()
                try:
                    await ticker
                except asyncio.CancelledError:
                    pass
        asyncio.run(check())


class JournalTests(unittest.TestCase):
    def test_real_crash_and_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            script = directory / "crashing-engine"
            script.write_text(
                "#!/usr/bin/env python3\nimport sys\n"
                "for line in sys.stdin:\n"
                "    if line.strip() == 'uci': print('id name CrashTest\\nuciok', flush=True)\n"
                "    elif line.strip() == 'isready': print('readyok', flush=True)\n"
                "    elif line.startswith('go'):\n"
                "        print('intentional test crash', file=sys.stderr, flush=True)\n"
                "        sys.exit(7)\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            game = Game(Settings(str(script), None, log_dir=directory))
            try:
                game.start()
                game.engine_turn()
                self.assertEqual(game.reason, "engine_failure")
                self.assertEqual(game.record.headers["Result"], "0-1")
            finally:
                game.close()
            self.assertIn("intentional test crash", game.path.with_suffix(".stderr.log").read_text())

    def test_start_failure_reaps_first_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            game = Game(Settings(str(ENGINE), str(directory / "missing"), log_dir=directory))
            try:
                with self.assertRaises(FileNotFoundError):
                    game.start()
            finally:
                game.close()
            self.assertIsNotNone(game.engines[chess.WHITE].process.poll())
            self.assertFalse(game.path.exists())

    def test_invalid_pgn(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.pgn"
            path.write_text('[SetUp "1"]\n[FEN "8/8/8/8/8/8/8/8 w - - 0 1"]\n\n*\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                list(read_games(path))

    def test_game_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            game = Game(Settings(str(ENGINE), str(ENGINE), 0.01, 2, Path(directory), 12))
            try:
                game.start()
                while not game.finished:
                    game.engine_turn()
                    loaded = list(read_games(game.path))[0]
                    self.assertEqual(loaded.end().board().fen(), game.board.fen())
                self.assertEqual(game.reason, "ply_limit")
                self.assertEqual(game.record.headers["Result"], "*")
                self.assertIn("TimeZone", loaded.headers)
                self.assertFalse(game.path.with_suffix(".pgn.tmp").exists())
            finally:
                game.close()

    def test_technical_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            game = Game(Settings(None, None, log_dir=Path(directory)))
            game.start()
            engine = Mock()
            engine.play.return_value = chess.engine.PlayResult(chess.Move.from_uci("e2e5"), None)
            game.engines[chess.WHITE] = engine
            game.engine_turn()
            self.assertEqual(game.record.headers["Result"], "0-1")
            self.assertEqual(game.reason, "engine_failure")
            self.assertEqual(game.board.ply(), 0)
            self.assertTrue(game.path.with_suffix(".stderr.log").exists())
            game.close()

    def test_export_builder_and_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            game = Game(Settings(None, None, log_dir=directory))
            game.start()
            for text in ["e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6", "h5f7"]:
                game.accept(chess.Move.from_uci(text))
            self.assertEqual(game.record.headers["Result"], "1-0")
            game.record.headers.update(White="ai_chess 0.1", Black="ai_chess 0.1", WhiteEngine=str(ENGINE), BlackEngine=str(ENGINE))
            game.save()
            export = directory / "games.txt"
            self.assertEqual(export_own_games(directory, export), 1)
            book = directory / "own.book"
            subprocess.run([str(BOOK), str(export), str(book)], check=True, capture_output=True)
            settings = Settings(None, None, None, 1, book=str(book), seed=5)
            engine = Engine(str(ENGINE), settings, directory / "stderr.log", "book")
            try:
                self.assertEqual(engine.play(chess.Board(), settings, "book").move.uci(), "e2e4")
            finally:
                engine.close()
                engine.wait_closed()
            game.close()


if __name__ == "__main__":
    unittest.main()
