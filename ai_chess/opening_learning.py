"""Online opening statistics; only verified own engine games supply rewards."""
from collections import Counter
import json
import math
from pathlib import Path

import chess

from .logs import eligible_own_game, read_games


def opening_candidates(scored, top=8, tolerance=50):
    ranked = sorted(scored, key=lambda item: (-item[1], item[0].uci()))
    if not ranked:
        raise ValueError('Нет оценённых дебютных ходов')
    best = ranked[0][1]
    return [move for move, score in ranked
            if (score == best if abs(best) >= 90000 else
                abs(score) < 90000 and best - score <= tolerance)][:top]


def position_key(board):
    # Same board, side, castling and legal en-passant rights share experience.
    return ' '.join(board.fen(en_passant='legal').split()[:4])


class OpeningLearner:
    def __init__(self, *, plies=12, top=8):
        if not 1 <= plies <= 20 or not 1 <= top <= 64:
            raise ValueError('Исследование: 1–20 полуходов и 1–64 кандидата')
        self.plies = plies
        self.top = top
        self.stats = {}
        self.cooldowns = {}
        self.trace = []
        self.switches = 0
        self.completed_games = 0
        self.history_games = 0

    def choose(self, board, candidates, rng):
        key = position_key(board)
        candidates = sorted(candidates, key=lambda move: move.uci())
        records = self.stats.get(key, {})
        total = sum(sum(records.get(move.uci(), [0, 0, 0])) for move in candidates)
        candidate_count = len(candidates)
        blocked = self.cooldowns.get(key)
        alternatives = [move for move in candidates if move.uci() != blocked]
        switched = bool(blocked and len(candidates) > 1 and len(alternatives) < len(candidates))
        if switched:
            candidates = alternatives
            self.switches += 1
            del self.cooldowns[key]
        counts = {move: sum(records.get(move.uci(), [0, 0, 0])) for move in candidates}
        untried = [move for move in candidates if not counts[move]]
        if untried:
            choices, reason = untried, 'untried'
        else:
            values = {}
            for move in candidates:
                wins, draws, losses = records[move.uci()]
                n = wins + draws + losses
                values[move] = (wins + .5 * draws + 1) / (n + 2) + .35 * math.sqrt(math.log1p(total) / (1 + n))
            best = max(values.values())
            choices, reason = [move for move in candidates if abs(values[move] - best) < 1e-12], 'ucb'
        move = rng.choice(choices)
        self.trace.append(dict(key=key, move=move.uci(), color=board.turn,
                               alternatives=candidate_count))
        return move, ('loss_switch; ' if switched else '') + reason

    def learn(self, game, *, cooldown=True, candidate_only=False):
        if not eligible_own_game(game):
            return False
        result = game.headers['Result']
        winner = None if result == '1/2-1/2' else result == '1-0'
        candidate = None
        if candidate_only:
            if (game.headers.get('FixedOpponentTraining') != 'true'
                    or game.headers.get('CandidateColor') not in ('white', 'black')):
                raise ValueError('Нет подтверждённой стороны кандидата в учебной партии')
            candidate = game.headers['CandidateColor'] == 'white'
        board = game.board()
        for index, move in enumerate(game.mainline_moves()):
            if index >= self.plies:
                break
            if not candidate_only or board.turn == candidate:
                record = self.stats.setdefault(position_key(board), {}).setdefault(move.uci(), [0, 0, 0])
                record[1 if winner is None else 0 if winner == board.turn else 2] += 1
            board.push(move)
        self.completed_games += 1
        if cooldown and winner is not None and (not candidate_only or winner != candidate):
            for item in self.trace:
                if item['color'] != winner and item['alternatives'] > 1:
                    self.cooldowns[item['key']] = item['move']
                    break
        return True

    def import_history(self, directory, engine_hash, seconds, destination):
        from .experiments import fingerprint
        directory = Path(directory)
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        if (not manifest.get('training') or manifest.get('comparison')
                or manifest.get('seconds_per_move') != seconds
                or any(manifest['participants'][side]['sha256'] != engine_hash
                       for side in ('white', 'black'))):
            raise ValueError('История должна быть учебной серией того же движка и времени на ход')
        lines, seen, sources = ['AI_CHESS_OWN_GAMES 1'], set(), []
        for path in sorted(directory.glob('*.pgn')):
            sources.append(dict(path=str(path.resolve()), sha256=fingerprint(path)))
            for game in read_games(path):
                if (game.headers.get('Experiment') != 'training'
                        or not eligible_own_game(game)):
                    continue
                moves = tuple(move.uci() for move in game.mainline_moves())
                if moves in seen:
                    continue
                seen.add(moves)
                self.learn(game, cooldown=False)
                lines.append(game.headers['Result'] + ' ' + ' '.join(moves))
        self.history_games = len(seen)
        destination.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return dict(directory=str(directory.resolve()), games=len(seen),
                    export=str(destination.resolve()), sha256=fingerprint(destination), sources=sources)

    def snapshot(self):
        return dict(version=1, plies=self.plies, top=self.top, tolerance_cp=50,
                    exploration_bonus=.35, completed_games=self.completed_games,
                    history_games=self.history_games, switches=self.switches,
                    stats=self.stats, cooldowns=self.cooldowns)

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if data['version'] != 1:
            raise ValueError('Неизвестная версия статистики дебютов')
        learner = cls(plies=data['plies'], top=data['top'])
        if any(len(record) != 3 or any(type(n) is not int or n < 0 for n in record)
               for moves in data['stats'].values() for record in moves.values()):
            raise ValueError('Некорректная статистика дебютов')
        learner.stats = data['stats']
        learner.cooldowns = data['cooldowns']
        for name in ('completed_games', 'history_games', 'switches'):
            value = data[name]
            if type(value) is not int or value < 0:
                raise ValueError('Некорректный счётчик статистики')
            setattr(learner, name, value)
        return learner


def opening_diversity(rows):
    sequences = []
    switches = 0
    for row in rows:
        if row.get('result') == '*' or row.get('reason') in {'engine_failure', 'startup_failure'}:
            continue
        for game in read_games(Path(row['path'])):
            sequences.append(tuple(move.uci() for move in game.mainline_moves()))
            switches += sum('loss_switch' in node.comment for node in game.mainline())
    return dict(finished_games=len(sequences), loss_switches=switches,
                first_moves=dict(Counter(moves[0] for moves in sequences if moves)),
                unique_prefixes={str(n): len({moves[:n] for moves in sequences}) for n in (4, 8, 12)})
