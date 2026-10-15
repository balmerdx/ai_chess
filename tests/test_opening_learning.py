"""Adaptive opening choices, verified rewards, checkpoints and filtered books."""
from pathlib import Path
import random
import subprocess
import tempfile
import unittest

import chess

from ai_chess.experiments import fingerprint, run_series, write_json
from ai_chess.game import Game, Settings
from ai_chess.logs import read_games
from ai_chess.opening_learning import OpeningLearner, opening_candidates, position_key

ROOT = Path(__file__).resolve().parents[1]
ENGINE = str(ROOT / 'build/release/ai_chess_engine')
BUILDER = str(ROOT / 'build/release/chess_book')


def scholar_game(directory):
    game = Game(Settings(None, None, log_dir=directory))
    game.start()
    try:
        for text in ('e2e4', 'e7e5', 'd1h5', 'b8c6', 'f1c4', 'g8f6', 'h5f7'):
            game.accept(chess.Move.from_uci(text))
        game.record.headers.update(WhiteEngine=ENGINE, BlackEngine=ENGINE,
                                   Experiment='training')
        game.save()
        return list(read_games(game.path))[0], game.path
    finally:
        game.close()


class OpeningLearningTests(unittest.TestCase):
    def test_candidates_include_classical_openings_and_separate_mates(self):
        scores = [('b1c3', 0), ('g1f3', 0), ('a2a4', -8), ('c2c4', -8),
                  ('d2d4', -8), ('e2e4', -8), ('f2f4', -8), ('h2h4', -8),
                  ('b2b4', -200)]
        candidates = opening_candidates([(chess.Move.from_uci(m), s) for m, s in scores])
        self.assertEqual(len(candidates), 8)
        self.assertTrue({'e2e4', 'd2d4', 'c2c4'} <= {m.uci() for m in candidates})
        a, b = candidates[:2]
        self.assertEqual(opening_candidates([(a, 99997), (b, 99995)]), [a])
        self.assertEqual(opening_candidates([(a, -99997), (b, -99995)]), [b])
        self.assertEqual(opening_candidates([(a, -89999), (b, -90001)]), [a])

    def test_loss_switch_is_once_and_survives_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            game, _ = scholar_game(directory)
            learner = OpeningLearner()
            board = chess.Board()
            white = chess.Move.from_uci('e2e4')
            learner.choose(board, [white], random.Random(1))
            board.push(white)
            moves = [chess.Move.from_uci(s) for s in ('e7e5', 'c7c5')]
            chosen, _ = learner.choose(board, moves, random.Random(0))
            self.assertEqual(chosen, moves[0])
            self.assertTrue(learner.learn(game))
            key = position_key(board)
            self.assertEqual(learner.stats[position_key(chess.Board())]['e2e4'], [1, 0, 0])
            self.assertEqual(learner.stats[key]['e7e5'], [0, 0, 1])
            write_json(directory / 'learning.json', learner.snapshot())
            restored = OpeningLearner.load(directory / 'learning.json')
            self.assertEqual(restored.snapshot(), learner.snapshot())
            # Single legal candidate must remain usable and retain the pending switch.
            self.assertEqual(restored.choose(board, [chosen], random.Random(1))[0], chosen)
            self.assertIn(key, restored.cooldowns)
            alternative, reason = restored.choose(board, moves, random.Random(1))
            self.assertNotEqual(alternative, chosen)
            self.assertIn('loss_switch', reason)
            self.assertNotIn(key, restored.cooldowns)
            self.assertEqual(restored.switches, 1)
            self.assertNotIn('loss_switch', restored.choose(board, moves, random.Random(1))[1])
            # The original move is eligible again, and can win the UCB choice.
            restored.stats[key][chosen.uci()] = [20, 0, 1]
            restored.stats[key][alternative.uci()] = [0, 0, 21]
            self.assertEqual(restored.choose(board, moves, random.Random(1))[0], chosen)

    def test_rewards_draws_and_unfinished_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            game, _ = scholar_game(Path(tmp))
            learner = OpeningLearner()
            for reason in ('engine_failure', 'duration_limit', 'ply_limit', 'aborted', 'startup_failure'):
                original = game.headers['Termination']
                game.headers['Termination'] = reason
                self.assertFalse(learner.learn(game))
                game.headers['Termination'] = original
            self.assertEqual(learner.stats, {})
            # Legal repetition supplies a real draw, rather than a synthetic result.
            match = Game(Settings(None, None, log_dir=Path(tmp)))
            match.start()
            try:
                for move in ('g1f3', 'g8f6', 'f3g1', 'f6g8', 'g1f3', 'g8f6', 'f3g1'):
                    match.accept(chess.Move.from_uci(move))
                match.record.headers.update(WhiteEngine=ENGINE, BlackEngine=ENGINE)
                match.save()
                draw = list(read_games(match.path))[0]
                self.assertTrue(learner.learn(draw))
                self.assertEqual(learner.stats[position_key(chess.Board())]['g1f3'], [0, 2, 0])
                self.assertFalse(learner.cooldowns)
            finally:
                match.close()

    def test_ucb_prefers_evidence_and_untried_and_is_seeded(self):
        board = chess.Board()
        moves = [chess.Move.from_uci(m) for m in ('e2e4', 'd2d4', 'c2c4')]
        learner = OpeningLearner()
        learner.stats[position_key(board)] = {'e2e4': [10, 0, 0], 'd2d4': [0, 0, 10]}
        self.assertEqual(learner.choose(board, moves, random.Random(1))[0], moves[2])
        self.assertEqual(learner.choose(board, moves[:2], random.Random(1))[0], moves[0])
        fresh = OpeningLearner()
        first, second = random.Random(42), random.Random(42)
        self.assertEqual([fresh.choose(board, moves, first)[0] for _ in range(20)],
                         [fresh.choose(board, moves, second)[0] for _ in range(20)])

    def test_history_deduplicates_and_checks_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _, path = scholar_game(directory)
            (directory / 'duplicate.pgn').write_bytes(path.read_bytes())
            manifest = dict(training=True, comparison=False, seconds_per_move=.05,
                            participants={s: {'sha256': fingerprint(ENGINE)} for s in ('white', 'black')})
            write_json(directory / 'manifest.json', manifest)
            learner = OpeningLearner()
            info = learner.import_history(directory, fingerprint(ENGINE), .05, directory / 'history.txt')
            self.assertEqual(info['games'], 1)
            self.assertEqual(learner.completed_games, 1)
            self.assertFalse(learner.cooldowns)
            self.assertEqual(len((directory / 'history.txt').read_text().splitlines()), 2)
            with self.assertRaisesRegex(ValueError, 'История'):
                learner.import_history(directory, 'wrong', .05, directory / 'bad.txt')
            manifest['comparison'] = True
            write_json(directory / 'manifest.json', manifest)
            with self.assertRaisesRegex(ValueError, 'История'):
                learner.import_history(directory, fingerprint(ENGINE), .05, directory / 'bad.txt')

    def test_builder_filters_samples_quality_depth_and_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source, output = directory / 'games.txt', directory / 'book'
            # Raw builder accepts own exported legal sequences; filtering is independently tested here.
            source.write_text('AI_CHESS_OWN_GAMES 1\n' +
                              '1-0 e2e4 e7e5\n' * 6 + '0-1 d2d4 d7d5\n' * 6 +
                              '1-0 c2c4 e7e5\n')
            command = [BUILDER, str(source), str(output)]
            subprocess.run(command + ['--max-plies', '1', '--min-games', '6', '--score-margin', '.05'], check=True, capture_output=True)
            records = output.read_text().splitlines()
            self.assertEqual(len(records), 2)
            self.assertEqual(records[1].split()[2:], ['6', '0', '0'])
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(len(output.read_text().splitlines()), 7)
            for options in (['--max-plies', '0'], ['--max-plies', '21'], ['--min-games', '-1'],
                            ['--score-margin', 'nan'], ['--score-margin', '2'], ['--unknown', '1']):
                self.assertEqual(subprocess.run(command + options, capture_output=True).returncode, 2)

    def test_real_adaptive_deadline_checkpoint_and_diversity(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            summary = run_series(Settings(ENGINE, ENGINE, .001, log_dir=directory),
                                 duration=.4, training=True, adaptive=True)
            self.assertEqual(summary['technical_errors'], 0)
            learner = OpeningLearner.load(directory / 'learning.json')
            self.assertEqual(learner.completed_games, summary['diversity']['finished_games'])
            self.assertEqual(learner.switches, summary['adaptive']['switches'])
            self.assertTrue((directory / 'manifest.json').exists())
            self.assertIn('duration_limit', summary['reasons'])


if __name__ == '__main__':
    unittest.main()
