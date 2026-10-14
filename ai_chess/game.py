"""Shared, GUI-independent game lifecycle and durable journals."""
from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import chess
import chess.engine
import chess.pgn

from .uci import Engine


@dataclass(frozen=True)
class Settings:
    white: str | None
    black: str | None
    seconds: float | None = 1.0
    depth: int | None = None
    log_dir: Path = Path("logs/games")
    max_plies: int = 400
    book: str | None = None
    seed: int = 1
    white_book: str | None = None
    black_book: str | None = None

    def for_side(self, color):
        override = self.white_book if color else self.black_book
        return replace(self, book=self.book if override is None else override)

    def validate(self) -> None:
        if self.seconds is not None and not 0 < self.seconds <= 86400:
            raise ValueError("Время на ход должно быть больше нуля и не больше суток")
        if self.depth is not None and not 1 <= self.depth <= 64:
            raise ValueError("Глубина должна быть от 1 до 64")
        if self.depth is None and self.seconds is None:
            raise ValueError("Укажите время или глубину")
        if self.max_plies < 1:
            raise ValueError("Предел полуходов должен быть положительным")
        if not 0 <= self.seed <= 2147483647:
            raise ValueError("Seed должен быть от 0 до 2147483647")


class Game:
    """Owned by a controller thread; interrupt() is safe from the GUI thread."""

    def __init__(self, settings: Settings):
        settings.validate()
        self.settings = settings
        self.board = chess.Board()
        self.record = chess.pgn.Game()
        self.node = self.record
        self.san: list[str] = []
        self.engines: dict[chess.Color, Engine] = {}
        self.lock = threading.Lock()
        self.interrupted = threading.Event()
        self.started = False
        self.finished = False
        self.reason = ""
        self.last_info = ""
        self.book_hits = {chess.WHITE: 0, chess.BLACK: 0}
        self.book_seconds = {chess.WHITE: 0.0, chess.BLACK: 0.0}
        self.id = uuid.uuid4().hex
        now = datetime.now().astimezone()
        self.path = settings.log_dir / f"{now:%Y%m%d_%H%M%S_%f}_{self.id[:8]}.pgn"
        self.record.headers.update({
            "Event": "ai_chess", "Site": "Local", "Date": now.strftime("%Y.%m.%d"),
            "Round": "1", "White": "Human", "Black": "Human", "Result": "*",
            "Time": now.strftime("%H:%M:%S"), "TimeZone": now.strftime("%z"),
            "StartedAt": now.isoformat(), "UTCDate": now.astimezone(timezone.utc).strftime("%Y.%m.%d"),
            "UTCTime": now.astimezone(timezone.utc).strftime("%H:%M:%S"),
            "MoveTime": str(settings.seconds), "SearchDepth": str(settings.depth),
            "MaxPlies": str(settings.max_plies), "Seed": str(settings.seed),
            "Source": "ai_chess-own", "Termination": "unterminated",
        })

    def start(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            for color, path in ((chess.WHITE, self.settings.white), (chess.BLACK, self.settings.black)):
                if path:
                    side_settings = self.settings.for_side(color)
                    engine = Engine(path, side_settings, self.path.with_suffix(".stderr.log"), "white" if color else "black")
                    with self.lock:
                        if self.interrupted.is_set():
                            engine.close()
                            raise InterruptedError("Запуск отменён")
                        self.engines[color] = engine
                    key = "White" if color else "Black"
                    self.record.headers[key] = engine.name
                    self.record.headers[key + "Engine"] = str(Path(path).resolve())
                    self.record.headers[key + "Book"] = str(Path(side_settings.book).resolve()) if side_settings.book else "off"
            self.started = True
            self.save()
        except BaseException:
            self.interrupt()
            raise

    def interrupt(self):
        self.interrupted.set()
        with self.lock:
            for engine in self.engines.values():
                engine.close()

    def close(self):
        self.interrupt()
        for engine in self.engines.values():
            engine.wait_closed()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".pgn.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(str(self.record) + "\n\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def finish(self, result: str, reason: str, diagnostic: str = ""):
        if self.finished:
            return
        self.finished = True
        self.reason = reason
        for color, key in ((chess.WHITE, "White"), (chess.BLACK, "Black")):
            self.record.headers[key + "BookHits"] = str(self.book_hits[color])
            self.record.headers[key + "BookSeconds"] = f"{self.book_seconds[color]:.6f}"
        self.record.headers.update(Result=result, Termination=reason, EndedAt=datetime.now().astimezone().isoformat())
        if diagnostic:
            with self.path.with_suffix(".stderr.log").open("a", encoding="utf-8") as stream:
                stream.write(diagnostic + "\n")
        self.save()

    def adjudicate(self):
        # claim_draw=True also recognizes a draw claim made by declaring the
        # next move, matching the chosen automatic-claim policy.
        outcome = self.board.outcome(claim_draw=True)
        if outcome:
            self.finish(outcome.result(), outcome.termination.name.lower())
        elif self.board.ply() >= self.settings.max_plies:
            self.finish("*", "ply_limit")

    def accept(self, move: chess.Move):
        if self.finished:
            raise ValueError("Партия завершена")
        if move not in self.board.legal_moves:
            raise ValueError(f"Нелегальный ход: {move.uci()}")
        san = self.board.san(move)
        self.board.push(move)
        self.san.append(san)
        self.node = self.node.add_variation(move)
        self.adjudicate()
        if not self.finished:
            self.save()

    def engine_turn(self):
        if self.finished or self.interrupted.is_set():
            return
        side = self.board.turn
        try:
            result = self.engines[side].play(self.board.copy(stack=True), self.settings, self.id)
            if self.interrupted.is_set():
                return
            if result.move is None:
                raise ValueError("Движок не вернул ход в незавершённой позиции")
            if result.move not in self.board.legal_moves:
                raise ValueError(f"Движок вернул нелегальный ход: {result.move.uci()}")
            if result.info.get("book"):
                self.book_hits[side] += 1
                self.book_seconds[side] += result.info.get("elapsed", 0.0)
            score = result.info.get("score")
            self.last_info = f"Глубина: {result.info.get('depth', '?')}  Оценка: {score.white() if score else '?'}"
        except (chess.engine.EngineError, TimeoutError, ValueError, OSError) as error:
            if not self.interrupted.is_set():
                self.finish("0-1" if side else "1-0", "engine_failure", str(error))
            return
        self.accept(result.move)

    def snapshot(self):
        return {
            "board": self.board.copy(stack=True), "san": tuple(self.san),
            "finished": self.finished, "reason": self.reason, "info": self.last_info,
            "result": self.record.headers["Result"], "path": str(self.path),
            "human_turn": self.board.turn not in self.engines,
        }
