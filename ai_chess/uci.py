"""Blocking UCI transport over pipes, with no asyncio/local-socket dependency.

One controller owns command/response I/O. stop() and close() may be called
from another thread. Reader threads drain both streams to avoid pipe deadlocks.
"""
from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path

import chess
import chess.engine


class Engine:
    def __init__(self, path, settings, diagnostic: Path, label: str):
        self.process = subprocess.Popen(
            [str(Path(path).resolve())], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.messages = queue.Queue()
        self.send_lock = threading.Lock()
        self.options = set()
        self.name = Path(path).name
        self.game_id = None
        self.stdout_thread = threading.Thread(target=self._stdout, daemon=True)
        self.stderr_thread = threading.Thread(target=self._stderr, args=(diagnostic, label), daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()
        try:
            self.send("uci")
            deadline = time.monotonic() + 5
            while True:
                line = self.receive(deadline)
                if line == "uciok":
                    break
                if line.startswith("id name "):
                    self.name = line[8:]
                elif line.startswith("option name ") and " type " in line:
                    self.options.add(line[12:].split(" type ", 1)[0].lower())
            if "seed" in self.options:
                self.send(f"setoption name Seed value {settings.seed}")
            if settings.book:
                if not {"bookfile", "ownbook"} <= self.options:
                    raise ValueError("Движок не поддерживает BookFile/OwnBook")
                self.send(f"setoption name BookFile value {Path(settings.book).resolve()}")
                self.send("setoption name OwnBook value true")
            self.ready()
        except BaseException:
            self.close()
            self.wait_closed()
            raise

    def _stdout(self):
        try:
            for line in self.process.stdout:
                self.messages.put(line.strip())
        finally:
            self.messages.put(None)
            self.process.stdout.close()
            self.process.wait()

    def _stderr(self, diagnostic, label):
        try:
            for line in self.process.stderr:
                try:
                    with diagnostic.open("a", encoding="utf-8") as stream:
                        stream.write(f"[{label}] {line.rstrip()}\n")
                except OSError:
                    pass
        finally:
            self.process.stderr.close()

    def send(self, line):
        if "\n" in line or "\r" in line:
            raise ValueError("Перевод строки в UCI-команде")
        with self.send_lock:
            try:
                if self.process.poll() is not None:
                    raise chess.engine.EngineTerminatedError("Процесс движка завершился")
                self.process.stdin.write(line + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as error:
                raise chess.engine.EngineTerminatedError(str(error)) from error

    def receive(self, deadline):
        if time.monotonic() > deadline:
            raise TimeoutError("Движок не ответил в установленное время")
        try:
            line = self.messages.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty as error:
            raise TimeoutError("Движок не ответил в установленное время") from error
        if line is None:
            raise chess.engine.EngineTerminatedError("Движок закрыл stdout")
        return line

    def ready(self):
        self.send("isready")
        deadline = time.monotonic() + 5
        while self.receive(deadline) != "readyok":
            pass

    @staticmethod
    def info(line, board):
        tokens = line.split()
        data = {}
        index = 1
        while index < len(tokens):
            token = tokens[index]
            try:
                if token in {"depth", "nodes", "nps", "seldepth"}:
                    data[token] = int(tokens[index + 1])
                    index += 2
                elif token == "time":
                    data["time"] = int(tokens[index + 1]) / 1000
                    index += 2
                elif token == "score":
                    kind, value = tokens[index + 1], int(tokens[index + 2])
                    if kind in {"cp", "mate"}:
                        score = chess.engine.Cp(value) if kind == "cp" else chess.engine.Mate(value)
                        data["score"] = chess.engine.PovScore(score, board.turn)
                    index += 3
                elif token == "pv":
                    position = board.copy(stack=False)
                    pv = []
                    for text in tokens[index + 1:]:
                        try:
                            move = position.parse_uci(text)
                        except ValueError:
                            break
                        pv.append(move)
                        position.push(move)
                    data["pv"] = pv
                    break
                elif token == "string":
                    break
                else:
                    index += 1
            except (IndexError, ValueError):
                break  # Diagnostic info must not invalidate an otherwise legal move.
        return data

    def play(self, board, settings, game_id):
        if game_id != self.game_id:
            self.send("ucinewgame")
            self.ready()
            self.game_id = game_id
        root = board.root()
        initial = "startpos" if root.fen() == chess.Board().fen() else "fen " + root.fen(en_passant="fen")
        moves = " moves " + " ".join(move.uci() for move in board.move_stack) if board.move_stack else ""
        self.send("position " + initial + moves)
        command = "go"
        if settings.depth is not None:
            command += f" depth {settings.depth}"
        if settings.seconds is not None:
            command += f" movetime {max(1, round(settings.seconds * 1000))}"
        self.send(command)
        deadline = time.monotonic() + (settings.seconds + 5 if settings.seconds is not None else 60)
        info = {}
        while True:
            line = self.receive(deadline)
            if line.startswith("info "):
                info.update(self.info(line, board))
            elif line.startswith("bestmove "):
                tokens = line.split()
                move = None if tokens[1] in {"0000", "(none)"} else chess.Move.from_uci(tokens[1])
                ponder = None
                if len(tokens) >= 4 and tokens[2] == "ponder":
                    try:
                        ponder = chess.Move.from_uci(tokens[3])
                    except ValueError:
                        pass
                return chess.engine.PlayResult(move, ponder, info)

    def stop(self):
        self.send("stop")

    def close(self):
        # Immediate, nonblocking cancellation from the GUI thread. Reader
        # threads drain EOF and reap the process. No search response is awaited.
        if self.process.poll() is None:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
        with self.send_lock:
            if not self.process.stdin.closed:
                try:
                    self.process.stdin.close()
                except BrokenPipeError:
                    pass

    def wait_closed(self):
        self.stdout_thread.join(timeout=2)
        self.stderr_thread.join(timeout=2)
