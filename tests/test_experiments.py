"""Experiment controls, real pipe cleanup, and isolation of book opponents."""
from dataclasses import replace
from pathlib import Path
import random
import tempfile
import time
import unittest
from unittest.mock import patch

import chess

from ai_chess.experiments import (audit_rows, build_training_book, choose_opening,
                                  fingerprint, paired_result, run_series, write_json)
from ai_chess.book_defaults import activate_book, default_book
from ai_chess.game import Game, Settings
from ai_chess.logs import export_own_games, read_games
from ai_chess.uci import Engine

ROOT = Path(__file__).resolve().parents[1]
ENGINE = str(ROOT / 'build/release/ai_chess_engine')
BUILDER = ROOT / 'build/release/chess_book'


class ExperimentTests(unittest.TestCase):
    def test_exploration_threshold_seed_and_mates(self):
        moves = [chess.Move.from_uci(s) for s in ['e2e4', 'd2d4', 'g1f3', 'b1c3', 'a2a4']]
        scored = list(zip(moves, [100, 80, 50, 49, -200]))
        first = random.Random(12)
        second = random.Random(12)
        choices = [choose_opening(scored, first) for _ in range(100)]
        self.assertEqual(choices, [choose_opening(scored, second) for _ in range(100)])
        self.assertEqual(set(choices), set(moves[:3]))
        self.assertEqual(choose_opening([(moves[0], 99995), (moves[1], 99997)], first), moves[1])
        self.assertEqual(choose_opening([(moves[0], -99995), (moves[1], -99997)], first), moves[0])

    def test_side_override_and_compatibility(self):
        settings = Settings(ENGINE, ENGINE, book='shared', white_book='', black_book='other')
        self.assertEqual(settings.for_side(chess.WHITE).book, '')
        self.assertEqual(settings.for_side(chess.BLACK).book, 'other')
        self.assertEqual(replace(settings, white_book=None).for_side(chess.WHITE).book, 'shared')

    def make_book(self, directory):
        games = directory / 'source'
        game = Game(Settings(None, None, log_dir=games))
        try:
            game.start()
            for text in ['e2e4', 'e7e5', 'd1h5', 'b8c6', 'f1c4', 'g8f6', 'h5f7']:
                game.accept(chess.Move.from_uci(text))
            game.record.headers.update(WhiteEngine=ENGINE, BlackEngine=ENGINE)
            game.save()
        finally:
            game.close()
        output = directory / 'test.book'
        summary = {'technical_errors': 0}
        book = build_training_book(summary, games, output, BUILDER)
        self.assertEqual(book['games'], 1)
        self.assertEqual(book['moves'], 7)
        return output

    def test_real_book_isolated_and_alternated(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            book = self.make_book(directory)
            settings = Settings(ENGINE, ENGINE, .001, log_dir=directory / 'match', max_plies=2,
                                seed=7, white_book=str(book), black_book='')
            summary = run_series(settings, games=2, comparison=True)
            self.assertEqual(summary['technical_errors'], 0)
            self.assertEqual([r['seed'] for r in summary['rows']], [7, 7])
            self.assertEqual([r['candidate_white'] for r in summary['rows']], [True, False])
            first, second = summary['rows']
            self.assertEqual(first['book_hits'], {'white': 1, 'black': 0})
            self.assertEqual(second['book_hits']['white'], 0)
            g1 = list(read_games(Path(first['path'])))[0]
            g2 = list(read_games(Path(second['path'])))[0]
            self.assertEqual(g1.headers['BlackBook'], 'off')
            self.assertEqual(g2.headers['WhiteBook'], 'off')
            self.assertEqual(g1.headers['WhiteBook'], g2.headers['BlackBook'])
            self.assertEqual(summary['comparison']['games'], 0)  # ply limits are not draws

    def test_deadline_cancels_real_search_and_reaps_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            processes = []
            original = Engine.__init__
            def capture(instance, *args, **kwargs):
                original(instance, *args, **kwargs)
                processes.append(instance)
            started = time.monotonic()
            with patch.object(Engine, '__init__', capture):
                summary = run_series(Settings(ENGINE, ENGINE, 10., log_dir=Path(tmp)), duration=.2)
            self.assertLess(time.monotonic()-started, 4)
            self.assertEqual(summary['technical_errors'], 0)
            self.assertEqual(summary['reasons'], {'duration_limit': 1})
            self.assertTrue(processes)
            for engine in processes:
                self.assertIsNotNone(engine.process.poll())
                self.assertFalse(engine.stdout_thread.is_alive())
                self.assertFalse(engine.stderr_thread.is_alive())
            export = Path(tmp) / 'export.txt'
            self.assertEqual(export_own_games(Path(tmp), export), 0)

    def test_training_pipeline_and_unfinished_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            summary = run_series(Settings(ENGINE, ENGINE, .001, log_dir=directory / 'train'),
                                 duration=.4, training=True)
            self.assertEqual(summary['technical_errors'], 0)
            self.assertIn('duration_limit', summary['reasons'])
            with self.assertRaisesRegex(ValueError, 'Нет подходящих'):
                build_training_book(summary, directory / 'train', directory / 'empty.book', BUILDER)
            self.assertFalse((directory / 'empty.book').exists())

    def test_pair_statistics_use_candidate_color_and_complete_pairs(self):
        rows = [dict(index=0, candidate_white=True, result='1-0', reason='checkmate'),
                dict(index=1, candidate_white=False, result='1-0', reason='checkmate'),
                dict(index=2, candidate_white=True, result='1/2-1/2', reason='threefold_repetition'),
                dict(index=3, candidate_white=False, result='*', reason='duration_limit'),
                dict(index=4, candidate_white=True, result='0-1', reason='engine_failure')]
        with patch('ai_chess.experiments.audit_rows', return_value={'unique_games': 2}):
            report = paired_result(rows, samples=200)
        self.assertEqual(report['complete_pairs'], 1)
        self.assertEqual(report['excluded_games'], 3)
        self.assertEqual(report['score'], .5)
        self.assertEqual((report['wins'], report['draws'], report['losses']), (1, 0, 1))
        self.assertEqual(report['by_color']['white']['score'], 1)
        self.assertEqual(report['by_color']['black']['score'], 0)
        self.assertFalse(report['confirmed_improvement'])

    def test_book_marker_and_fallback(self):
        self.assertEqual(Engine.info('info string book move', chess.Board()), {'book': True})
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            book = self.make_book(directory)
            settings = Settings(None, None, None, 2, book=str(book))
            engine = Engine(ENGINE, settings, directory / 'stderr.log', 'test')
            try:
                self.assertTrue(engine.play(chess.Board(), settings, 'book').info['book'])
                board = chess.Board()
                board.push_uci('a2a3')
                response = engine.play(board, settings, 'book')
                self.assertFalse(response.info.get('book', False))
                self.assertIn(response.move, board.legal_moves)
                self.assertGreater(response.info['depth'], 0)
            finally:
                engine.close()
                engine.wait_closed()

    def test_promotion_requires_sample_diversity_and_no_failures(self):
        rows = [dict(index=i, candidate_white=i % 2 == 0,
                     result='1-0' if i % 2 == 0 else '0-1', reason='checkmate')
                for i in range(50)]
        with patch('ai_chess.experiments.audit_rows', return_value={'unique_games': 20}):
            self.assertTrue(paired_result(rows, samples=200)['confirmed_improvement'])
            self.assertFalse(paired_result(rows[:10], samples=200)['confirmed_improvement'])
            failed = rows + [dict(index=50, candidate_white=True, result='0-1', reason='engine_failure')]
            self.assertFalse(paired_result(failed, samples=200)['confirmed_improvement'])
        with patch('ai_chess.experiments.audit_rows', return_value={'unique_games': 1}):
            self.assertFalse(paired_result(rows, samples=200)['confirmed_improvement'])

    def test_default_book_is_pinned_and_can_be_explicitly_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            engine, book, config = [directory / name for name in ['engine', 'book', 'default.json']]
            engine.write_bytes(b'evaluated engine')
            book.write_bytes(b'evaluated book')
            write_json(directory / 'manifest.json', {'participants': {'white': {
                'sha256': fingerprint(engine), 'book_sha256': fingerprint(book)}}})
            summary = dict(comparison={'confirmed_improvement': True}, technical_errors=0,
                           book_usage={'hits': 5}, rows=[{'path': str(directory / 'game.pgn')}])
            self.assertTrue(activate_book(engine, book, summary, config))
            self.assertEqual(default_book(engine, config), str(book))
            settings = Settings(str(engine), str(engine), book=default_book(engine, config), black_book='')
            self.assertEqual(settings.for_side(chess.BLACK).book, '')
            book.write_bytes(b'changed book')
            self.assertIsNone(default_book(engine, config))
            with self.assertRaisesRegex(ValueError, 'изменились'):
                activate_book(engine, book, summary, config)

    def test_unconfirmed_result_never_creates_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'default.json'
            self.assertFalse(activate_book('missing', 'missing', {'comparison': {'confirmed_improvement': False}}, config))
            self.assertFalse(config.exists())
            config.write_text('{broken json')
            self.assertIsNone(default_book('missing', config))


if __name__ == '__main__':
    unittest.main()
