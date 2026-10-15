"""Timed, headless self-play, opening exploration and paired book evaluation."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import random
import subprocess
import threading
import time

import chess
import chess.engine

from .game import Game, Settings
from .logs import export_own_games, read_games
from .opening_learning import OpeningLearner, opening_candidates, opening_diversity
from .uci import Engine


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def fingerprint(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def choose_opening(scored, rng):
    """Keep at most three near-best moves; never mix different mate outcomes."""
    return rng.choice(opening_candidates(scored, top=3))


def explore(game: Game, rng, deadline, learner=None, analysis_engine=None):
    """Score child positions using our own search, then accept a seeded choice."""
    scored = []
    board = game.board
    engine = analysis_engine if analysis_engine is not None else game.engines[board.turn]
    analysis = replace(game.settings, seconds=None, depth=3, book=None)
    for move in sorted(board.legal_moves, key=lambda m: m.uci()):
        if game.interrupted.is_set() or time.monotonic() >= deadline:
            return
        child = board.copy(stack=True)
        child.push(move)
        outcome = child.outcome(claim_draw=True)
        if outcome is not None:
            value = 0 if outcome.winner is None else (100000 if outcome.winner == board.turn else -100000)
        else:
            response = engine.play(child, analysis, game.id)
            score = response.info.get('score')
            if score is None:
                raise ValueError('Исследование дебюта: движок не прислал оценку')
            value = score.pov(board.turn).score(mate_score=100000)
        scored.append((move, value))
    if not game.interrupted.is_set() and time.monotonic() < deadline:
        reason = 'random'
        if learner is None:
            move = choose_opening(scored, rng)
        else:
            move, reason = learner.choose(board, opening_candidates(scored, learner.top), rng)
        game.accept(move)
        game.node.comment = (f'Opening exploration: own search, depth 3, top '
                             f'{learner.top if learner else 3} within 50 cp; {reason}')
        game.save()


def audit_rows(rows):
    sequences = []
    openings = []
    for row in rows:
        if 'path' not in row:
            continue
        games = list(read_games(Path(row['path'])))
        if len(games) != 1:
            raise ValueError(f"Ожидалась одна партия: {row['path']}")
        game = games[0]
        if row['result'] != '*' and row['reason'] != 'engine_failure':
            outcome = game.end().board().outcome(claim_draw=True)
            if outcome is None or outcome.result() != row['result']:
                raise ValueError(f"Результат не соответствует позиции: {row['path']}")
        moves = tuple(m.uci() for m in game.mainline_moves())
        sequences.append(moves)
        openings.append(moves[:20])
    return dict(unique_games=len(set(sequences)), unique_openings=len(set(openings)))


def paired_result(rows, samples=10000):
    pairs = {}
    for row in rows:
        if row['result'] == '*' or row['reason'] in {'engine_failure', 'startup_failure'}:
            continue
        score = .5 if row['result'] == '1/2-1/2' else float((row['result'] == '1-0') == row['candidate_white'])
        pairs.setdefault(row['index'] // 2, []).append((row, score))
    complete = [pair for pair in pairs.values() if len(pair) == 2]
    scores = [sum(item[1] for item in pair) / 2 for pair in complete]
    used = [item for pair in complete for item in pair]
    result = dict(complete_pairs=len(scores), games=len(used),
                  excluded_games=len(rows) - len(used),
                  wins=sum(score == 1 for _, score in used),
                  draws=sum(score == .5 for _, score in used),
                  losses=sum(score == 0 for _, score in used))
    result['by_color'] = {}
    for color, white in [('white', True), ('black', False)]:
        values = [score for row, score in used if row['candidate_white'] == white]
        result['by_color'][color] = dict(games=len(values), score=sum(values) / len(values) if values else None)
    result['score'] = sum(scores) / len(scores) if scores else None
    if scores:
        rng = random.Random(20261014)
        means = sorted(sum(rng.choice(scores) for _ in scores) / len(scores) for _ in range(samples))
        result['paired_bootstrap_95'] = [means[int(samples * .025)], means[min(samples-1, int(samples * .975))]]
    else:
        result['paired_bootstrap_95'] = None
    result['unique_games'] = audit_rows([row for row, _ in used])['unique_games']
    no_errors = not any(row['reason'] in {'engine_failure', 'startup_failure'} for row in rows)
    # A constant bootstrap sample has a degenerate interval; require a conservative
    # bounded-observation check as well before changing application defaults.
    lower = (result['score'] - math.sqrt(math.log(20) / (2 * len(scores)))) if scores else None
    result['hoeffding_lower_95'] = lower
    result['confirmed_improvement'] = bool(no_errors and len(scores) >= 20 and result['unique_games'] >= 10
                                           and result['paired_bootstrap_95'][0] > .5 and lower > .5)
    result['interpretation'] = ('Улучшение подтверждено в условиях этого эксперимента' if result['confirmed_improvement']
                                else 'Убедительное улучшение не подтверждено; оценка предварительная')
    return result


def run_series(settings: Settings, *, duration=None, games=None, alternate=True,
               training=False, comparison=False, adaptive=False, opening_plies=12,
               opening_top=8, history=None, fixed_opponent=False):
    settings.validate()
    if (adaptive and (not training or comparison)) or (history and not adaptive):
        raise ValueError('Адаптация и история доступны только для обучения')
    learner = OpeningLearner(plies=opening_plies, top=opening_top) if adaptive else None
    if fixed_opponent and (not adaptive or history):
        raise ValueError('Фиксированный соперник требует адаптивного обучения без истории само­игры')
    if adaptive and ((not fixed_opponent and settings.white != settings.black) or not settings.white or not settings.black or
                     any(settings.for_side(side).book for side in (True, False))):
        raise ValueError('Адаптивное обучение требует одинаковых движков без готовой книги')
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise ValueError('Длительность должна быть конечной и положительной')
    if games is not None and games < 1:
        raise ValueError('Количество партий должно быть положительным')
    if duration is None and games is None:
        games = 1
    directory = settings.log_dir
    directory.mkdir(parents=True, exist_ok=True)
    summary_path = directory / 'summary.json'
    if summary_path.exists() or any(directory.glob('*.pgn')):
        raise ValueError(f'Для воспроизводимости требуется новый каталог партий: {directory}')
    history_info = None
    if history:
        history_info = learner.import_history(history, fingerprint(settings.white), settings.seconds,
                                              directory / 'history-games.txt')
    started = time.monotonic()
    deadline = started + duration if duration is not None else math.inf
    rows = []
    manifest = dict(duration_seconds=duration, games_limit=games, seconds_per_move=settings.seconds,
                    depth=settings.depth, max_plies=settings.max_plies, seed=settings.seed,
                    training=training, comparison=comparison,
                    fixed_opponent=fixed_opponent,
                    exploration=dict(plies=opening_plies if adaptive else 4, depth=3,
                                     top=opening_top if adaptive else 3, tolerance_cp=50,
                                     adaptive=adaptive, exploration_bonus=.35 if adaptive else None) if training else None,
                    history=history_info,
                    participants={})
    for color, path in [('white', settings.white), ('black', settings.black)]:
        side = settings.for_side(color == 'white')
        manifest['participants'][color] = dict(engine=str(Path(path).resolve()) if path else None,
            sha256=fingerprint(path) if path else None, book=str(Path(side.book).resolve()) if side.book else None,
            book_sha256=fingerprint(side.book) if side.book else None)
    write_json(directory / 'manifest.json', manifest)
    if learner:
        write_json(directory / 'learning.json', learner.snapshot())
    index = 0
    while time.monotonic() < deadline and (games is None or index < games):
        swapped = alternate and index % 2 == 1
        seed = (settings.seed + (index // 2 if comparison or fixed_opponent else index)) % 2147483648
        current = replace(settings, seed=seed)
        if swapped:
            current = replace(current, white=settings.black, black=settings.white,
                              white_book=settings.black_book, black_book=settings.white_book)
        game = Game(current)
        timer = threading.Timer(max(0, deadline-time.monotonic()), game.interrupt) if duration is not None else None
        if timer:
            timer.daemon = True
            timer.start()
        startup_error = None
        teacher = None
        try:
            game.start()
            game.record.headers.update(Round=str(index+1), Experiment='training' if training else 'comparison' if comparison else 'match',
                                       Pair=str(index // 2 + 1), CandidateColor='black' if swapped else 'white')
            if fixed_opponent:
                game.record.headers['FixedOpponentTraining'] = 'true'
                teacher = Engine(settings.white, replace(current, book=None, white_book=None, black_book=None),
                                 game.path.with_suffix('.analysis.stderr.log'), 'analysis')
                game.add_helper(teacher)
            rng = random.Random(seed)
            if learner:
                learner.trace = []
            while not game.finished and not game.interrupted.is_set() and time.monotonic() < deadline:
                if (training and game.board.ply() < (opening_plies if adaptive else 4)
                        and (not fixed_opponent or game.board.turn == (not swapped))):
                    try:
                        explore(game, rng, deadline, learner, teacher)
                    except (chess.engine.EngineError, TimeoutError, ValueError, OSError) as error:
                        if not game.interrupted.is_set():
                            game.finish('0-1' if game.board.turn else '1-0', 'engine_failure', str(error))
                else:
                    game.engine_turn()
            if not game.finished:
                game.finish('*', 'duration_limit')
        except KeyboardInterrupt:
            if game.started and not game.finished:
                game.finish('*', 'aborted')
            raise
        except Exception as error:
            if game.interrupted.is_set() and time.monotonic() >= deadline:
                if game.started and not game.finished:
                    game.finish('*', 'duration_limit')
            else:
                startup_error = str(error)
                if game.started and not game.finished:
                    game.finish('*', 'startup_failure', startup_error)
        finally:
            if timer:
                timer.cancel()
                timer.join()
            game.close()
        if game.started:
            row = dict(index=index, seed=seed, candidate_white=not swapped,
                       result=game.record.headers['Result'], reason=game.reason, plies=game.board.ply(), path=str(game.path),
                       book_hits={key: game.book_hits[color] for color, key in [(chess.WHITE, 'white'), (chess.BLACK, 'black')]},
                       book_seconds={key: game.book_seconds[color] for color, key in [(chess.WHITE, 'white'), (chess.BLACK, 'black')]})
        elif startup_error:
            row = dict(index=index, seed=seed, candidate_white=not swapped, result='*', reason='startup_failure', plies=0, error=startup_error)
        else:
            break
        rows.append(row)
        if learner and game.started:
            verified = list(read_games(game.path))
            if len(verified) != 1:
                raise ValueError('Ожидалась одна партия для обновления статистики')
            learner.learn(verified[0], candidate_only=fixed_opponent)
            write_json(directory / 'learning.json', learner.snapshot())
        print(json.dumps(row, ensure_ascii=False), flush=True)
        write_json(directory / 'progress.json', dict(elapsed_seconds=time.monotonic()-started, rows=rows))
        index += 1
        if startup_error or ((adaptive or comparison) and row['reason'] == 'engine_failure'):
            break
    summary = dict(elapsed_seconds=time.monotonic()-started, games=len(rows), plies=sum(r['plies'] for r in rows),
                   reasons=dict(Counter(r['reason'] for r in rows)), results=dict(Counter(r['result'] for r in rows)), rows=rows)
    summary.update(audit_rows(rows))
    summary['technical_errors'] = sum(r['reason'] in {'engine_failure', 'startup_failure'} for r in rows)
    summary['nonempty_stderr'] = [str(p) for p in directory.glob('*.stderr.log') if p.stat().st_size]
    if learner:
        summary['adaptive'] = dict(plies=opening_plies, top=opening_top, history=history_info,
                                   fixed_opponent=fixed_opponent,
                                   history_games=learner.history_games, switches=learner.switches,
                                   statistics=str((directory / 'learning.json').resolve()))
        summary['diversity'] = opening_diversity(rows)
    if comparison:
        summary['comparison'] = paired_result(rows)
        hits = sum(sum(r.get('book_hits', {}).values()) for r in rows)
        elapsed = sum(sum(r.get('book_seconds', {}).values()) for r in rows)
        summary['book_usage'] = dict(hits=hits, elapsed_seconds=elapsed,
            nominal_saved_seconds=max(0., hits * settings.seconds - elapsed) if settings.seconds is not None else None)
    write_json(summary_path, summary)
    return summary


def build_training_book(summary, directory: Path, output: Path, builder, *, min_games=6):
    if summary['technical_errors']:
        raise ValueError('Сбор завершился техническими ошибками; исправьте их до построения книги')
    export = directory / 'own-games.txt'
    adaptive = summary.get('adaptive')
    count = export_own_games(directory, export, candidate_only=bool(adaptive and adaptive.get('fixed_opponent')))
    if adaptive and adaptive.get('history'):
        source = adaptive['history']
        if fingerprint(source['export']) != source['sha256']:
            raise ValueError('История изменилась после обучения')
        historical = Path(source['export']).read_text(encoding='utf-8').splitlines()[1:]
        export.write_text('AI_CHESS_OWN_GAMES 1\n' + '\n'.join(historical + export.read_text().splitlines()[1:]) + '\n', encoding='utf-8')
        count += len(historical)
    if not count:
        raise ValueError('Нет подходящих законченных партий: книга не построена')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + '.tmp')
    command = [str(Path(builder).resolve()), str(export), str(temporary)]
    if adaptive:
        command += ['--max-plies', str(adaptive['plies']), '--min-games', str(min_games), '--score-margin', '0.05']
    result = subprocess.run(command,
                            check=True, text=True, capture_output=True)
    lines = temporary.read_text().splitlines()
    if not lines or lines[0] != 'AI_CHESS_BOOK 1' or len(lines) == 1:
        raise ValueError('Сборщик создал пустую или некорректную книгу')
    temporary.replace(output)
    book = dict(games=count, positions=len({line.split()[0] for line in lines[1:]}),
                moves=len(lines)-1, path=str(output.resolve()), sha256=fingerprint(output), builder_output=result.stdout.strip())
    summary['book'] = book
    write_json(directory / 'summary.json', summary)
    print(json.dumps(book, ensure_ascii=False), flush=True)
    return book
