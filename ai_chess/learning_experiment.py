"""Run the approved adaptive training and frozen comparison as one experiment."""
import argparse
import math
from pathlib import Path
import shutil
import time

from .book_defaults import activate_book
from .experiments import build_training_book, fingerprint, run_series, write_json
from .game import Settings


def run_experiment(directory, *, engine, builder, history=None, training_seconds=2400,
                   comparison_seconds=1200, seconds=.05, seed=20261014):
    if any(not math.isfinite(value) or value <= 0
           for value in (training_seconds, comparison_seconds, seconds)):
        raise ValueError('Длительность и время на ход должны быть конечными и положительными')
    directory, engine, builder = Path(directory).resolve(), Path(engine).resolve(), Path(builder).resolve()
    Settings(str(engine), str(engine), seconds=seconds, seed=seed).validate()
    if not engine.is_file() or not builder.is_file():
        raise ValueError('Не найдены движок или сборщик книги')
    if directory.exists() and any(directory.iterdir()):
        raise ValueError('Эксперимент требует пустого каталога')
    baseline_dir = directory / 'baseline'
    baseline_dir.mkdir(parents=True)
    baseline, saved_builder = baseline_dir / 'ai_chess_engine', baseline_dir / 'chess_book'
    shutil.copy2(engine, baseline)
    shutil.copy2(builder, saved_builder)
    root = Path(__file__).resolve().parents[1]
    sources = [*sorted((root / 'ai_chess').glob('*.py')), *sorted((root / 'cpp').glob('*.*'))]
    manifest = dict(version=1, training_seconds=training_seconds, comparison_seconds=comparison_seconds,
                    seconds_per_move=seconds, seed=seed, engine=str(engine),
                    engine_sha256=fingerprint(engine), baseline=str(baseline),
                    baseline_sha256=fingerprint(baseline), builder_sha256=fingerprint(saved_builder),
                    history=str(Path(history).resolve()) if history else None,
                    sources={str(path.relative_to(root)): fingerprint(path) for path in sources})
    write_json(directory / 'manifest.json', manifest)
    started = time.monotonic()
    status = dict(stage='training', activated=False)
    write_json(directory / 'status.json', status)
    try:
        training = run_series(Settings(str(engine), str(engine), seconds,
                                       log_dir=directory / 'training', seed=seed),
                              duration=training_seconds, training=True, adaptive=True, history=history)
        if training['technical_errors'] or training['nonempty_stderr']:
            raise ValueError('Обучение завершилось техническими ошибками или непустым stderr')
        book = directory / 'candidate.book'
        build_training_book(training, directory / 'training', book, saved_builder)
        if fingerprint(engine) != manifest['engine_sha256']:
            raise ValueError('Движок изменился во время обучения')
        status.update(stage='comparison', book=str(book), book_sha256=fingerprint(book))
        write_json(directory / 'status.json', status)
        comparison = run_series(Settings(str(engine), str(baseline), seconds,
                                         log_dir=directory / 'comparison',
                                         seed=(seed + 1000000) % 2147483648,
                                         white_book=str(book), black_book=''),
                                duration=comparison_seconds, comparison=True)
        if comparison['technical_errors'] or comparison['nonempty_stderr']:
            raise ValueError('Сравнение завершилось техническими ошибками или непустым stderr')
        status.update(stage='complete', activated=activate_book(engine, book, comparison),
                      training=training, comparison=comparison)
    except BaseException as error:
        status.update(stage='failed', error=str(error))
        raise
    finally:
        status['elapsed_seconds'] = time.monotonic() - started
        write_json(directory / 'status.json', status)
    return status


def main():
    parser = argparse.ArgumentParser(description='Адаптивное обучение дебютам и независимое сравнение')
    parser.add_argument('--logs', type=Path, required=True)
    parser.add_argument('--engine', default='build/release/ai_chess_engine')
    parser.add_argument('--builder', default='build/release/chess_book')
    parser.add_argument('--history', type=Path)
    parser.add_argument('--training-seconds', type=float, default=2400)
    parser.add_argument('--comparison-seconds', type=float, default=1200)
    parser.add_argument('--time', type=float, default=.05)
    parser.add_argument('--seed', type=int, default=20261014)
    args = parser.parse_args()
    try:
        status = run_experiment(args.logs, engine=args.engine, builder=args.builder,
                                history=args.history, training_seconds=args.training_seconds,
                                comparison_seconds=args.comparison_seconds,
                                seconds=args.time, seed=args.seed)
        print(status['comparison']['comparison'], flush=True)
        print(f"Книга подключена по умолчанию: {status['activated']}", flush=True)
    except KeyboardInterrupt:
        parser.exit(130, 'Эксперимент прерван\n')
    except Exception as error:
        parser.exit(1, f'Ошибка эксперимента: {error}\n')


if __name__ == '__main__':
    main()
