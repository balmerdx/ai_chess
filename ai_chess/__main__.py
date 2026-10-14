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
    play.add_argument("--games", type=int, default=1)
    play.add_argument("--time", type=float, default=None, help="Секунд на ход (по умолчанию 1, если не задана глубина)")
    play.add_argument("--depth", type=int)
    play.add_argument("--max-plies", type=int, default=400)
    play.add_argument("--logs", type=Path, default=Path("logs/games"))
    play.add_argument("--book")
    play.add_argument("--seed", type=int, default=1)
    play.add_argument("--no-alternate", action="store_true", help="Не менять движки местами между партиями")
    export = sub.add_parser("export-book", help="Экспорт собственных законченных партий для C++-сборщика")
    export.add_argument("--logs", type=Path, default=Path("logs/games"))
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
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
    elif args.command == "match":
        if args.games < 1:
            parser.error("--games должен быть положительным")
        failures = 0
        for index in range(args.games):
            white, black = args.white, args.black
            if index % 2 and not args.no_alternate:
                white, black = black, white
            seconds = args.time if args.time is not None else (None if args.depth is not None else 1.0)
            game = None
            try:
                game = Game(Settings(white, black, seconds, args.depth, args.logs, args.max_plies, args.book, (args.seed + index) % 2147483648))
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
