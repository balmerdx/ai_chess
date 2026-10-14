"""Qt interface; engine I/O and journaling run exclusively in a worker."""
from __future__ import annotations

import queue
import threading
from pathlib import Path

import chess
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QMainWindow, QMessageBox, QPushButton, QSpinBox, QSplitter, QTabWidget,
    QVBoxLayout, QWidget,
)

from .game import Game, Settings
from .logs import read_games
from .qt_runtime import ensure_gui_runtime


class BoardWidget(QWidget):
    chosen = Signal(str)

    def __init__(self):
        super().__init__()
        self.board = chess.Board()
        self.flipped = False
        self.playable = False
        self.selected = None
        self.setMinimumSize(400, 400)

    def position(self, board, playable=False):
        self.board = board.copy(stack=True)
        self.playable = playable
        self.selected = None
        self.update()

    def geometry_values(self):
        cell = (min(self.width(), self.height()) - 36) / 8
        return cell, (self.width() - cell * 8) / 2, (self.height() - cell * 8) / 2

    def coordinates(self, square):
        file, rank = chess.square_file(square), chess.square_rank(square)
        return (7 - file, rank) if self.flipped else (file, 7 - rank)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cell, left, top = self.geometry_values()
        painter.fillRect(self.rect(), QColor("#272d35"))
        last = self.board.peek() if self.board.move_stack else None
        targets = {m.to_square for m in self.board.legal_moves if m.from_square == self.selected} if self.selected is not None else set()
        for square in chess.SQUARES:
            x, y = self.coordinates(square)
            px, py = left + x * cell, top + y * cell
            color = QColor("#eadcc4" if (square // 8 + square % 8) % 2 else "#809879")
            if last and square in (last.from_square, last.to_square):
                color = QColor("#c8bd67")
            if square == self.selected:
                color = QColor("#e9b44c")
            painter.fillRect(int(px), int(py), int(cell + 1), int(cell + 1), color)
            if square in targets:
                painter.setBrush(QColor(30, 70, 70, 100))
                painter.setPen(Qt.PenStyle.NoPen)
                radius = int(cell * 0.13)
                painter.drawEllipse(int(px + cell / 2 - radius), int(py + cell / 2 - radius), radius * 2, radius * 2)
            piece = self.board.piece_at(square)
            if piece:
                painter.setPen(QColor("#161b22"))
                font = QFont("DejaVu Sans")
                font.setPixelSize(int(cell * 0.78))
                painter.setFont(font)
                painter.drawText(int(px), int(py), int(cell), int(cell), Qt.AlignmentFlag.AlignCenter, piece.unicode_symbol())
        painter.setPen(QPen(QColor("#e8e8e8")))
        painter.setFont(QFont("Sans", 10))
        for index in range(8):
            file = 7 - index if self.flipped else index
            rank = index if self.flipped else 7 - index
            painter.drawText(int(left + index * cell), int(top + 8 * cell), int(cell), 18, Qt.AlignmentFlag.AlignCenter, chr(97 + file))
            painter.drawText(int(left - 18), int(top + index * cell), 16, int(cell), Qt.AlignmentFlag.AlignCenter, str(rank + 1))

    def mousePressEvent(self, event):
        if not self.playable or event.button() != Qt.MouseButton.LeftButton:
            return
        cell, left, top = self.geometry_values()
        x, y = int((event.position().x() - left) // cell), int((event.position().y() - top) // cell)
        if not (0 <= x < 8 and 0 <= y < 8):
            return
        square = chess.square(7 - x, y) if self.flipped else chess.square(x, 7 - y)
        if self.selected is not None:
            candidates = [m for m in self.board.legal_moves if m.from_square == self.selected and m.to_square == square]
            if candidates:
                move = candidates[0]
                if len(candidates) > 1:
                    names = {chess.QUEEN: "Ферзь", chess.ROOK: "Ладья", chess.BISHOP: "Слон", chess.KNIGHT: "Конь"}
                    choice, ok = QInputDialog.getItem(self, "Превращение", "Выберите фигуру", list(names.values()), 0, False)
                    if not ok:
                        return
                    move = next(m for m in candidates if names[m.promotion] == choice)
                self.playable = False
                self.selected = None
                self.chosen.emit(move.uci())
                self.update()
                return
        piece = self.board.piece_at(square)
        self.selected = square if piece and piece.color == self.board.turn else None
        self.update()


class GameWorker(QThread):
    changed = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, generation, settings, delay):
        super().__init__()
        self.generation = generation
        self.settings = settings
        self.delay = delay
        self.commands = queue.Queue()
        self.cancelled = threading.Event()
        self.game = None

    def stop(self):
        self.cancelled.set()
        self.commands.put(("stop", None))
        if self.game:
            self.game.interrupt()

    def run(self):
        game = None
        try:
            game = self.game = Game(self.settings)
            if self.cancelled.is_set():
                return
            game.start()
            paused = False
            self.changed.emit(self.generation, game.snapshot())
            while not self.cancelled.is_set() and not game.finished:
                human = game.board.turn not in game.engines
                timeout = None if human or paused else self.delay
                try:
                    command, value = self.commands.get(timeout=timeout)
                    if command == "stop":
                        break
                    if command == "pause":
                        paused = bool(value)
                    elif command == "move" and human:
                        try:
                            game.accept(chess.Move.from_uci(value))
                        except ValueError as error:
                            self.failed.emit(self.generation, str(error))
                    self.changed.emit(self.generation, game.snapshot())
                except queue.Empty:
                    game.engine_turn()
                    if not self.cancelled.is_set():
                        self.changed.emit(self.generation, game.snapshot())
            if game.started and not game.finished:
                game.finish("*", "aborted")
        except Exception as error:
            if not self.cancelled.is_set():
                self.failed.emit(self.generation, str(error))
            if game and game.started and not game.finished:
                try:
                    game.finish("*", "aborted", str(error))
                except OSError:
                    pass
        finally:
            if game:
                game.close()


def button(text, action, layout):
    widget = QPushButton(text)
    widget.clicked.connect(action)
    layout.addWidget(widget)
    return widget


class Window(QMainWindow):
    def __init__(self, engine: str, logs: Path):
        super().__init__()
        self.setWindowTitle("ai_chess — шахматы")
        self.resize(1080, 740)
        self.logs = logs
        self.generation = 0
        self.worker = None
        self.workers = []
        self.replay_positions = []
        self.replay_index = 0
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.replay_next)
        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        play_tab, replay_tab = QWidget(), QWidget()
        tabs.addTab(play_tab, "Партия")
        tabs.addTab(replay_tab, "Журналы и просмотр")
        play = QHBoxLayout(play_tab)
        self.board = BoardWidget()
        self.board.chosen.connect(self.human_move)
        play.addWidget(self.board, 3)
        side = QVBoxLayout()
        play.addLayout(side, 2)
        form = QFormLayout()
        side.addLayout(form)
        self.mode = QComboBox()
        self.mode.addItems(["Человек за белых", "Человек за чёрных", "Два движка"])
        form.addRow("Режим", self.mode)
        self.white = QLineEdit(engine)
        self.black = QLineEdit(engine)
        for title, field in (("Белый движок", self.white), ("Чёрный движок", self.black)):
            row = QHBoxLayout()
            row.addWidget(field)
            button("…", lambda checked=False, f=field: self.pick_engine(f), row)
            form.addRow(title, row)
        self.seconds = QDoubleSpinBox()
        self.seconds.setRange(0, 86400)
        self.seconds.setDecimals(3)
        self.seconds.setValue(1)
        self.seconds.setSpecialValueText("Без лимита времени")
        form.addRow("Секунд на ход", self.seconds)
        self.depth = QSpinBox()
        self.depth.setRange(0, 64)
        self.depth.setSpecialValueText("Без лимита глубины")
        form.addRow("Глубина (полуходы)", self.depth)
        self.delay = QDoubleSpinBox()
        self.delay.setRange(0, 10)
        self.delay.setValue(0.3)
        form.addRow("Задержка показа, с", self.delay)
        self.book = QLineEdit()
        self.book.setPlaceholderText("Необязательно: путь к книге")
        form.addRow("Дебютная книга", self.book)
        self.seed = QSpinBox()
        self.seed.setRange(0, 2147483647)
        self.seed.setValue(1)
        form.addRow("Seed", self.seed)
        controls = QHBoxLayout()
        side.addLayout(controls)
        button("Новая партия", self.new_game, controls)
        button("Остановить", self.stop_game, controls)
        self.pause = QCheckBox("Пауза между ходами движков")
        self.pause.toggled.connect(self.set_pause)
        side.addWidget(self.pause)
        self.flip = QCheckBox("Перевернуть доску")
        self.flip.toggled.connect(self.flip_board)
        side.addWidget(self.flip)
        self.status = QLabel("Выберите режим и начните партию")
        self.status.setWordWrap(True)
        side.addWidget(self.status)
        self.moves = QListWidget()
        side.addWidget(self.moves, 1)
        self.make_replay(replay_tab)

    def pick_engine(self, field):
        path, _ = QFileDialog.getOpenFileName(self, "Выберите исполняемый файл движка")
        if path:
            field.setText(path)

    def flip_board(self, value):
        self.board.flipped = value
        self.board.update()

    def new_game(self):
        try:
            mode = self.mode.currentIndex()
            settings = Settings(
                None if mode == 0 else self.white.text().strip(),
                None if mode == 1 else self.black.text().strip(),
                self.seconds.value() or None, self.depth.value() or None, self.logs,
                book=self.book.text().strip() or None, seed=self.seed.value(),
            )
            settings.validate()
            if (mode != 0 and not settings.white) or (mode != 1 and not settings.black):
                raise ValueError("Укажите путь к движку")
        except ValueError as error:
            QMessageBox.warning(self, "Настройки", str(error))
            return
        if self.worker:
            self.worker.stop()
        self.generation += 1
        self.pause.setChecked(False)
        self.flip.setChecked(mode == 1)
        self.board.position(chess.Board())
        self.moves.clear()
        self.status.setText("Запуск движков…")
        worker = GameWorker(self.generation, settings, self.delay.value())
        self.worker = worker
        self.workers.append(worker)
        worker.changed.connect(self.update_game)
        worker.failed.connect(self.worker_error)
        worker.finished.connect(lambda w=worker: self.worker_done(w))
        worker.start()

    def worker_done(self, worker):
        if worker in self.workers:
            self.workers.remove(worker)
        if self.worker is worker:
            self.worker = None
            self.board.playable = False
        worker.deleteLater()

    def stop_game(self):
        if self.worker:
            self.worker.stop()
        self.generation += 1
        self.board.playable = False
        self.status.setText("Партия остановлена; журнал сохраняется")

    def set_pause(self, value):
        if self.worker:
            self.worker.commands.put(("pause", value))

    def human_move(self, uci):
        if self.worker:
            self.worker.commands.put(("move", uci))

    def update_game(self, generation, data):
        if generation != self.generation:
            return
        self.board.position(data["board"], data["human_turn"] and not data["finished"])
        self.moves.clear()
        self.moves.addItems([f"{index // 2 + 1}{'.' if index % 2 == 0 else '…'} {san}" for index, san in enumerate(data["san"])])
        self.moves.scrollToBottom()
        state = f"{data['result']} — {data['reason']}" if data["finished"] else ("Ваш ход" if data["human_turn"] else "Ход движка")
        self.status.setText(f"{state}\n{data['info']}\nЖурнал: {data['path']}")

    def worker_error(self, generation, message):
        if generation == self.generation:
            self.status.setText("Ошибка: " + message)
            QMessageBox.warning(self, "Партия", message)

    def make_replay(self, tab):
        layout = QHBoxLayout(tab)
        self.replay_board = BoardWidget()
        layout.addWidget(self.replay_board, 3)
        side = QVBoxLayout()
        layout.addLayout(side, 2)
        controls = QHBoxLayout()
        side.addLayout(controls)
        button("Обновить журналы", self.refresh_logs, controls)
        button("Открыть PGN…", self.open_pgn, controls)
        self.games = QListWidget()
        self.games.currentRowChanged.connect(self.select_game)
        side.addWidget(self.games)
        self.replay_status = QLabel("Выберите партию")
        self.replay_status.setWordWrap(True)
        side.addWidget(self.replay_status)
        navigation = QHBoxLayout()
        side.addLayout(navigation)
        button("|◀", lambda: self.show_ply(0), navigation)
        button("◀", lambda: self.show_ply(self.replay_index - 1), navigation)
        button("▶", self.replay_next, navigation)
        button("▶|", lambda: self.show_ply(len(self.replay_positions) - 1), navigation)
        self.auto = button("Авто / пауза", self.toggle_replay, navigation)
        self.replay_moves = QListWidget()
        self.replay_moves.currentRowChanged.connect(lambda row: self.show_ply(row + 1) if row >= 0 else None)
        side.addWidget(self.replay_moves)
        self.replay_games = []
        self.refresh_logs()

    def add_pgn(self, path):
        try:
            for game in read_games(path):
                self.replay_games.append((path, game))
                headers = game.headers
                self.games.addItem(f"{headers.get('Date', '?')} {headers.get('Time', '')} | {headers.get('White')} — {headers.get('Black')} | {headers.get('Result')}")
        except (OSError, ValueError) as error:
            self.replay_games.append((path, None))
            self.games.addItem(f"Ошибка {path.name}: {error}")

    def refresh_logs(self):
        self.timer.stop()
        self.games.clear()
        self.replay_games = []
        self.replay_positions = []
        self.replay_moves.clear()
        self.replay_board.position(chess.Board())
        self.replay_status.setText("Выберите партию")
        for path in sorted(self.logs.glob("*.pgn"), reverse=True):
            self.add_pgn(path)

    def open_pgn(self):
        path, _ = QFileDialog.getOpenFileName(self, "Открыть PGN", str(self.logs), "PGN (*.pgn);;Все файлы (*)")
        if path:
            first = len(self.replay_games)
            self.add_pgn(Path(path))
            self.games.setCurrentRow(first)

    def select_game(self, row):
        self.timer.stop()
        if row < 0 or row >= len(self.replay_games):
            return
        path, game = self.replay_games[row]
        if game is None:
            self.replay_positions = []
            self.replay_status.setText(f"Невозможно прочитать {path}")
            return
        board = game.board()
        self.replay_positions = [board.copy(stack=True)]
        labels = []
        for move in game.mainline_moves():
            labels.append(f"{board.fullmove_number}{'.' if board.turn else '…'} {board.san(move)}")
            board.push(move)
            self.replay_positions.append(board.copy(stack=True))
        self.replay_moves.blockSignals(True)
        self.replay_moves.clear()
        self.replay_moves.addItems(labels)
        self.replay_moves.blockSignals(False)
        self.replay_status.setText(f"{path}\n{game.headers.get('StartedAt', '')}\n{game.headers.get('Termination', '')}")
        self.show_ply(0)

    def show_ply(self, ply):
        if not self.replay_positions:
            return
        self.replay_index = max(0, min(ply, len(self.replay_positions) - 1))
        self.replay_board.position(self.replay_positions[self.replay_index])
        self.replay_moves.blockSignals(True)
        self.replay_moves.setCurrentRow(self.replay_index - 1)
        self.replay_moves.blockSignals(False)
        if self.replay_index == len(self.replay_positions) - 1:
            self.timer.stop()

    def replay_next(self):
        self.show_ply(self.replay_index + 1)

    def toggle_replay(self):
        if self.timer.isActive():
            self.timer.stop()
        elif self.replay_positions:
            if self.replay_index == len(self.replay_positions) - 1:
                self.show_ply(0)
            self.timer.start()

    def closeEvent(self, event):
        self.timer.stop()
        for worker in list(self.workers):
            worker.stop()
        if any(worker.isRunning() for worker in self.workers):
            event.ignore()
            self.status.setText("Завершение процессов и сохранение журналов…")
            QTimer.singleShot(100, self.close)
        else:
            event.accept()


def run(engine: str, logs: Path):
    ensure_gui_runtime()
    app = QApplication([])
    window = Window(engine, logs)
    window.show()
    app.exec()
