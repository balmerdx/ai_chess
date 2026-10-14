import argparse
from pathlib import Path

from .game import Game, Settings
from .logs import export_own_games

DEFAULT_ENGINE = "build/release/ai_chess_engine"


def main():
    parser = argparse.ArgumentParser(description="Шахматы: человек, UCI-движки и просмотр PGN")
    sub = parser.add_subparsers(dest="command")
    gui = sub.add_parser("gui", help="Графический интерфейс (режим по умолчанию)")
    gui.add_argument("--engine", default=DEFAULT_ENGINE)
    gui.add_argument("--logs", type=Path, default=Path("logs/games"))
    play = sub.add_parser("match", help="Серия партий без окна")
    play.add_argument("--white", default=DEFAULT_ENGINE)
    play.add_argument("--black", default=DEFAULT_ENGINE)
    play.add_argument("--games", type=int)
    play.add_argument("--duration", type=float, help="Длительность серии в секундах; нужен новый каталог --logs")
    play.add_argument("--time", type=float, default=None, help="Секунд на ход (по умолчанию 1, если не задана глубина)")
    play.add_argument("--depth", type=int)
    play.add_argument("--max-plies", type=int, default=400)
    play.add_argument("--logs", type=Path, default=Path("logs/games"))
    play.add_argument("--book")
    play.add_argument("--white-book", help="Книга первого участника; пустая строка отключает её")
    play.add_argument("--black-book", help="Книга второго участника; пустая строка отключает её")
    play.add_argument("--seed", type=int, default=1)
    play.add_argument("--no-alternate", action="store_true", help="Не менять движки местами между партиями")
    export = sub.add_parser("export-book", help="Экспорт собственных законченных партий для C++-сборщика")
    export.add_argument("--logs", type=Path, default=Path("logs/games"))
    export.add_argument("--output", type=Path, required=True)
    train = sub.add_parser("train-book", help="Сбор разнообразных собственных партий и построение книги")
    train.add_argument("--engine", default=DEFAULT_ENGINE)
    train.add_argument("--builder", default="build/release/chess_book")
    train.add_argument("--duration", type=float, default=1200)
    train.add_argument("--time", type=float, default=.05)
    train.add_argument("--max-plies", type=int, default=400)
    train.add_argument("--seed", type=int, default=731)
    train.add_argument("--logs", type=Path, required=True)
    train.add_argument("--output", type=Path, required=True)
    compare = sub.add_parser("compare-book", help="Парный матч книги против сохранённого исходного движка")
    compare.add_argument("--engine", default=DEFAULT_ENGINE)
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--book", required=True)
    compare.add_argument("--duration", type=float, default=1200)
    compare.add_argument("--time", type=float, default=.05)
    compare.add_argument("--max-plies", type=int, default=400)
    compare.add_argument("--seed", type=int, default=12031)
    compare.add_argument("--logs", type=Path, required=True)
    compare.add_argument("--activate-if-better", action="store_true", help="Подключить стандартную книгу только при подтверждённом улучшении")
    args = parser.parse_args()
    if args.command == "match" and args.book is None:
        from .book_defaults import default_book
        if args.white_book is None:
            args.white_book = default_book(args.white)
        if args.black_book is None:
            args.black_book = default_book(args.black)
    if args.command in {None, "gui"}:
        from .qt_runtime import QtRuntimeError, ensure_gui_runtime
        try:
            ensure_gui_runtime(allow_reexec=True)
        except QtRuntimeError as error:
            parser.exit(2, f"Ошибка GUI: {error}\n")
        from .gui import run
        run(getattr(args, "engine", DEFAULT_ENGINE), getattr(args, "logs", Path("logs/games")))
    elif args.command == "export-book":
        try:
            count = export_own_games(args.logs, args.output)
            print(f"Экспортировано партий: {count}; {args.output}")
        except (OSError, ValueError) as error:
            parser.exit(1, f"Ошибка: {error}\n")
    elif args.command in {"train-book", "compare-book"} or (args.command == "match" and args.duration is not None):
        from .experiments import build_training_book, run_series
        try:
            if args.command == "train-book":
                settings = Settings(args.engine, args.engine, args.time, log_dir=args.logs,
                                    max_plies=args.max_plies, seed=args.seed)
                summary = run_series(settings, duration=args.duration, training=True)
                build_training_book(summary, args.logs, args.output, args.builder)
            elif args.command == "compare-book":
                settings = Settings(args.engine, args.baseline, args.time, log_dir=args.logs,
                                    max_plies=args.max_plies, seed=args.seed, white_book=args.book, black_book="")
                summary = run_series(settings, duration=args.duration, comparison=True)
                print(summary['comparison'])
                if args.activate_if_better:
                    from .book_defaults import activate_book
                    active = activate_book(args.engine, args.book, summary)
                    print(f"Книга подключена по умолчанию: {active}")
            else:
                seconds = args.time if args.time is not None else (None if args.depth is not None else 1.)
                settings = Settings(args.white, args.black, seconds, args.depth, args.logs, args.max_plies,
                                    args.book, args.seed, args.white_book, args.black_book)
                summary = run_series(settings, duration=args.duration, games=args.games, alternate=not args.no_alternate)
            if summary['technical_errors']:
                parser.exit(1, f"Технических ошибок: {summary['technical_errors']}\n")
        except KeyboardInterrupt:
            parser.exit(130, "Серия прервана\n")
        except Exception as error:
            parser.exit(1, f"Ошибка эксперимента: {error}\n")
    elif args.command == "match":
        if args.games is None:
            args.games = 1
        if args.games < 1:
            parser.error("--games должен быть положительным")
        failures = 0
        for index in range(args.games):
            white, black = args.white, args.black
            white_book, black_book = args.white_book, args.black_book
            if index % 2 and not args.no_alternate:
                white, black = black, white
                white_book, black_book = black_book, white_book
            seconds = args.time if args.time is not None else (None if args.depth is not None else 1.0)
            game = None
            try:
                game = Game(Settings(white, black, seconds, args.depth, args.logs, args.max_plies, args.book, (args.seed + index) % 2147483648, white_book, black_book))
                game.start()
                while not game.finished:
                    game.engine_turn()
                print(f"{index+1}/{args.games}: {game.record.headers['Result']} {game.reason} ({game.board.ply()} полуходов) {game.path}")
                failures += game.reason == "engine_failure"
            except KeyboardInterrupt:
                if game and game.started and not game.finished:
                    game.finish("*", "aborted")
                parser.exit(130, "Серия прервана\n")
            except Exception as error:
                parser.exit(1, f"Ошибка запуска или записи: {error}\n")
            finally:
                if game:
                    game.close()
        if failures:
            parser.exit(1, f"Технических поражений: {failures}\n")


if __name__ == "__main__":
    main()
