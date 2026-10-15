"""Deadline-bounded training against a frozen opponent and held-out evaluation."""
import argparse
from datetime import datetime, timezone
import math
from pathlib import Path
import shutil
import subprocess
import time

from .book_defaults import activate_book
from .experiments import build_training_book, fingerprint, run_series, write_json
from .game import Settings


def run_fixed_experiment(directory, *, engine, builder, deadline_utc,
                         opponent=None, selection_seconds=1800, final_seconds=2700,
                         audit_reserve=120, seconds=.05, seed=20261015,
                         min_games=6, activate_if_better=True):
    deadline_wall = datetime.fromisoformat(deadline_utc.replace('Z', '+00:00'))
    if deadline_wall.tzinfo is None:
        raise ValueError('У предельного времени должен быть часовой пояс')
    remaining = deadline_wall.timestamp() - time.time()
    if any(not math.isfinite(x) or x <= 0 for x in (selection_seconds, final_seconds, audit_reserve, seconds)):
        raise ValueError('Временные бюджеты должны быть конечными и положительными')
    if type(min_games) is not int or min_games < 1:
        raise ValueError('Минимум результатов должен быть положительным целым')
    training_seconds = remaining - selection_seconds - final_seconds - audit_reserve
    if training_seconds <= 1:
        raise ValueError('Недостаточно времени для обучения и независимой проверки')
    started = time.monotonic()
    deadline = started + remaining
    directory = Path(directory).resolve()
    engine = Path(engine).resolve()
    opponent = Path(opponent or engine).resolve()
    builder = Path(builder).resolve()
    Settings(str(engine), str(opponent), seconds=seconds, seed=seed).validate()
    if not all(p.is_file() for p in (engine, opponent, builder)):
        raise ValueError('Не найдены бинарники')
    if directory.exists() and any(directory.iterdir()):
        raise ValueError('Требуется пустой каталог эксперимента')
    frozen = directory / 'frozen'
    frozen.mkdir(parents=True)
    copies = {}
    for name, source in [('candidate', engine), ('opponent', opponent), ('builder', builder)]:
        path = frozen / name
        shutil.copy2(source, path)
        copies[name] = dict(path=str(path), sha256=fingerprint(path), source=str(source))
    root = Path(__file__).resolve().parents[1]
    sources = [*sorted((root / 'ai_chess').glob('*.py')), *sorted((root / 'cpp').glob('*.*'))]
    manifest = dict(version=1, started_at_utc=datetime.now(timezone.utc).isoformat(),
                    deadline_utc=deadline_utc, remaining_at_start=remaining,
                    training_seconds=training_seconds, selection_seconds=selection_seconds,
                    final_seconds=final_seconds, audit_reserve=audit_reserve,
                    activate_if_better=activate_if_better,
                    seconds_per_move=seconds, seed=seed, binaries=copies,
                    sources={str(p.relative_to(root)): fingerprint(p) for p in sources},
                    policy=dict(fixed_opponent=True, candidate_only_statistics=True,
                                separate_analysis_process=True, plies=12, top=8,
                                book_depths=[4, 12], min_games=min_games, score_margin=.05))
    write_json(directory / 'manifest.json', manifest)
    status = dict(stage='training', activated=False, deadline_utc=deadline_utc)
    write_json(directory / 'status.json', status)

    def check_integrity():
        if any(fingerprint(v['path']) != v['sha256'] for v in copies.values()):
            raise ValueError('Замороженный бинарник изменился')
        if any(fingerprint(root / p) != digest for p, digest in manifest['sources'].items()):
            raise ValueError('Исходники изменились во время эксперимента')

    def check_run(summary):
        if summary['technical_errors'] or summary['nonempty_stderr']:
            raise ValueError('Техническая ошибка или непустой stderr; эксперимент остановлен')

    def bounded_duration(requested):
        budget = min(requested, deadline - time.monotonic() - audit_reserve)
        if budget <= 0:
            raise TimeoutError('Исчерпан общий бюджет эксперимента')
        return budget

    try:
        candidate, baseline, saved_builder = [copies[name]['path'] for name in ('candidate', 'opponent', 'builder')]
        training = run_series(Settings(candidate, baseline, seconds, log_dir=directory / 'training', seed=seed),
                              duration=bounded_duration(training_seconds), training=True, adaptive=True,
                              fixed_opponent=True)
        check_run(training)
        check_integrity()
        deep = directory / 'depth12.book'
        build_training_book(training, directory / 'training', deep, saved_builder, min_games=min_games)
        shallow = directory / 'depth4.book'
        subprocess.run([saved_builder, str(directory / 'training/own-games.txt'), str(shallow),
                        '--max-plies', '4', '--min-games', str(min_games), '--score-margin', '.05'],
                       check=True, capture_output=True, text=True, timeout=30)
        if len(shallow.read_text().splitlines()) <= 1:
            raise ValueError('Короткая книга оказалась пустой')
        status.update(stage='selection', training=training,
                      books={str(p): fingerprint(p) for p in (shallow, deep)}, selections=[])
        write_json(directory / 'status.json', status)
        for number, book in enumerate((shallow, deep)):
            check_integrity()
            summary = run_series(Settings(candidate, baseline, seconds,
                                          log_dir=directory / f'selection-depth{4 if number == 0 else 12}',
                                          seed=(seed + 2000000) % 2147483648,
                                          white_book=str(book), black_book=''),
                                 duration=bounded_duration(selection_seconds / 2), comparison=True)
            check_run(summary)
            status['selections'].append(dict(book=str(book), sha256=fingerprint(book), summary=summary))
            write_json(directory / 'status.json', status)
        eligible = [item for item in status['selections'] if item['summary']['comparison']['score'] is not None]
        if not eligible:
            raise ValueError('Нет полных пар для выбора книги')
        # Prefer the shorter book on a tie. These scores select; they do not promote.
        winner = max(eligible, key=lambda item: item['summary']['comparison']['score'])
        book = Path(winner['book'])
        status.update(stage='comparison', selected_book=str(book), book_sha256=fingerprint(book))
        write_json(directory / 'status.json', status)
        check_integrity()
        comparison = run_series(Settings(candidate, baseline, seconds, log_dir=directory / 'comparison',
                                         seed=(seed + 3000000) % 2147483648,
                                         white_book=str(book), black_book=''),
                                duration=bounded_duration(final_seconds), comparison=True)
        check_run(comparison)
        check_integrity()
        if fingerprint(book) != status['book_sha256']:
            raise ValueError('Книга изменилась во время итоговой проверки')
        activated = bool(activate_if_better and activate_book(engine, book, comparison))
        status.update(stage='complete', activated=activated, comparison=comparison)
    except BaseException as error:
        status.update(stage='failed', error=str(error))
        raise
    finally:
        status['elapsed_seconds'] = time.monotonic() - started
        write_json(directory / 'status.json', status)
    return status


def main():
    parser = argparse.ArgumentParser(description='Обучение против фиксированного движка с общим сроком завершения')
    parser.add_argument('--logs', type=Path, required=True)
    parser.add_argument('--engine', default='build/release/ai_chess_engine')
    parser.add_argument('--opponent')
    parser.add_argument('--builder', default='build/release/chess_book')
    parser.add_argument('--deadline-utc', required=True, help='Предельное время ISO 8601 с часовым поясом')
    parser.add_argument('--selection-seconds', type=float, default=1800)
    parser.add_argument('--final-seconds', type=float, default=2700)
    parser.add_argument('--audit-reserve', type=float, default=120)
    parser.add_argument('--time', type=float, default=.05)
    parser.add_argument('--seed', type=int, default=20261015)
    parser.add_argument('--min-games', type=int, default=6)
    parser.add_argument('--no-activate', action='store_true', help='Сохранить результат без изменения стандартной книги')
    args = parser.parse_args()
    try:
        status = run_fixed_experiment(args.logs, engine=args.engine, opponent=args.opponent,
                                      builder=args.builder, deadline_utc=args.deadline_utc,
                                      selection_seconds=args.selection_seconds, final_seconds=args.final_seconds,
                                      audit_reserve=args.audit_reserve, seconds=args.time, seed=args.seed,
                                      min_games=args.min_games, activate_if_better=not args.no_activate)
        print(status['comparison']['comparison'], flush=True)
        print(f"Книга подключена по умолчанию: {status['activated']}", flush=True)
    except KeyboardInterrupt:
        parser.exit(130, 'Эксперимент прерван\n')
    except Exception as error:
        parser.exit(1, f'Ошибка эксперимента: {error}\n')


if __name__ == '__main__':
    main()
