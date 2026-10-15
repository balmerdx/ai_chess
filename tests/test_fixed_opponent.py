"""Fixed-opponent isolation, candidate rewards and cancellation of analysis."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import chess

from ai_chess import experiments
from ai_chess.fixed_experiment import run_fixed_experiment
from ai_chess.game import Settings
from ai_chess.logs import export_own_games, read_games
from ai_chess.opening_learning import OpeningLearner, position_key
from ai_chess.uci import Engine
from test_opening_learning import scholar_game, ENGINE, BUILDER


class FixedOpponentTests(unittest.TestCase):
    def test_candidate_only_rewards_do_not_learn_opponent_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            game, _ = scholar_game(Path(tmp))
            game.headers.update(FixedOpponentTraining='true', CandidateColor='black')
            learner = OpeningLearner()
            board = chess.Board()
            board.push_uci('e2e4')
            learner.choose(board, [chess.Move.from_uci(m) for m in ('e7e5', 'c7c5')], random.Random(0))
            self.assertTrue(learner.learn(game, candidate_only=True))
            self.assertNotIn(position_key(chess.Board()), learner.stats)
            self.assertEqual(learner.stats[position_key(board)]['e7e5'], [0, 0, 1])
            self.assertEqual(learner.cooldowns[position_key(board)], 'e7e5')
            self.assertEqual(sum(len(moves) for moves in learner.stats.values()), 3)
            winner = OpeningLearner()
            game.headers['CandidateColor'] = 'white'
            self.assertTrue(winner.learn(game, candidate_only=True))
            self.assertEqual(winner.stats[position_key(chess.Board())]['e2e4'], [1, 0, 0])
            self.assertFalse(winner.cooldowns)
            del game.headers['CandidateColor']
            with self.assertRaisesRegex(ValueError, 'кандидата'):
                winner.learn(game, candidate_only=True)

    def test_version_two_export_and_cpp_builder_exclude_opponent(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            game, path = scholar_game(directory)
            game.headers.update(FixedOpponentTraining='true', CandidateColor='black')
            path.write_text(str(game) + '\n\n')
            export, book = directory / 'export.txt', directory / 'candidate.book'
            self.assertEqual(export_own_games(directory, export, candidate_only=True), 1)
            self.assertEqual(export.read_text().splitlines()[0], 'AI_CHESS_OWN_GAMES 2')
            self.assertTrue(export.read_text().splitlines()[1].startswith('1-0 black '))
            subprocess.run([BUILDER, str(export), str(book)], check=True, capture_output=True)
            records = book.read_text().splitlines()[1:]
            self.assertEqual(len(records), 3)
            self.assertTrue(all(line.split()[2:] == ['0', '0', '1'] for line in records))
            game.headers['CandidateColor'] = 'white'
            path.write_text(str(game) + '\n\n')
            export_own_games(directory, export, candidate_only=True)
            subprocess.run([BUILDER, str(export), str(book)], check=True, capture_output=True)
            self.assertEqual(len(book.read_text().splitlines()) - 1, 4)
            self.assertTrue(all(line.split()[2:] == ['1', '0', '0'] for line in book.read_text().splitlines()[1:]))
            del game.headers['FixedOpponentTraining']
            path.write_text(str(game) + '\n\n')
            self.assertEqual(export_own_games(directory, export, candidate_only=True), 0)
            export.write_text('AI_CHESS_OWN_GAMES 2\n1-0 green e2e4\n')
            self.assertEqual(subprocess.run([BUILDER, str(export), str(book)], capture_output=True).returncode, 2)

    def test_only_candidate_explores_with_a_separate_teacher(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen, teachers = [], []
            original = experiments.explore
            def capture(game, rng, deadline, learner=None, analysis_engine=None):
                candidate = game.record.headers['CandidateColor'] == 'white'
                self.assertEqual(game.board.turn, candidate)
                self.assertIsNotNone(analysis_engine)
                self.assertIsNot(analysis_engine, game.engines[candidate])
                seen.append(candidate)
                teachers.append(analysis_engine)
                return original(game, rng, deadline, learner, analysis_engine)
            with patch.object(experiments, 'explore', side_effect=capture):
                summary = experiments.run_series(Settings(ENGINE, ENGINE, .001, log_dir=Path(tmp), max_plies=4),
                                                  games=2, training=True, adaptive=True, fixed_opponent=True)
            self.assertEqual(summary['technical_errors'], 0)
            self.assertEqual(seen, [True, True, False, False])
            self.assertEqual([row['seed'] for row in summary['rows']], [1, 1])
            for row in summary['rows']:
                game = next(read_games(Path(row['path'])))
                candidate = game.headers['CandidateColor'] == 'white'
                for index, node in enumerate(game.mainline()):
                    self.assertEqual(bool(node.comment), (index % 2 == 0) == candidate)
                self.assertEqual(game.headers['WhiteBook'], 'off')
                self.assertEqual(game.headers['BlackBook'], 'off')
            self.assertTrue(all(teacher.process.poll() is not None for teacher in teachers))

    def test_deadline_reaps_all_three_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            instances = []
            original = Engine.__init__
            def capture(instance, *args, **kwargs):
                original(instance, *args, **kwargs)
                instances.append(instance)
            with patch.object(Engine, '__init__', capture):
                summary = experiments.run_series(Settings(ENGINE, ENGINE, 10., log_dir=Path(tmp)),
                                                  duration=.3, training=True, adaptive=True, fixed_opponent=True)
            self.assertEqual(summary['technical_errors'], 0)
            self.assertEqual(len(instances), 3)
            for instance in instances:
                self.assertIsNotNone(instance.process.poll())
                self.assertFalse(instance.stdout_thread.is_alive())
                self.assertFalse(instance.stderr_thread.is_alive())

    def test_history_and_invalid_budget_are_rejected_before_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'not-created'
            with self.assertRaises(ValueError):
                experiments.run_series(Settings(ENGINE, ENGINE, log_dir=directory), duration=1,
                                       training=True, adaptive=True, fixed_opponent=True, history='old')
            self.assertFalse(directory.exists())
            past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            with self.assertRaisesRegex(ValueError, 'Недостаточно'):
                run_fixed_experiment(directory, engine=ENGINE, builder=BUILDER, deadline_utc=past)
            self.assertFalse(directory.exists())


if __name__ == '__main__':
    unittest.main()
