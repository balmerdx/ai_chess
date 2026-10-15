"""Strict PGN loading and export of this application's own completed games."""
from pathlib import Path

import chess
import chess.pgn


def read_games(path: Path):
    with path.open(encoding="utf-8-sig") as stream:
        while (game := chess.pgn.read_game(stream)) is not None:
            if game.errors:
                raise ValueError(f"{path.name}: {game.errors[0]}")
            board = game.board()
            if not board.is_valid():
                raise ValueError(f"{path.name}: некорректная начальная позиция")
            for move in game.mainline_moves():
                if move not in board.legal_moves:
                    raise ValueError(f"{path.name}: нелегальный ход {move.uci()}")
                board.push(move)
            yield game


def eligible_own_game(game) -> bool:
    """Accept only independently verified, completed engine-vs-engine games."""
    if (game.headers.get("Source") != "ai_chess-own"
            or "WhiteEngine" not in game.headers or "BlackEngine" not in game.headers
            or game.board().fen() != chess.Board().fen()
            or game.headers.get("Termination") in {
                "engine_failure", "aborted", "duration_limit", "ply_limit",
                "unterminated", "startup_failure"}
            or game.headers.get("Result", "*") not in {"1-0", "0-1", "1/2-1/2"}):
        return False
    outcome = game.end().board().outcome(claim_draw=True)
    if outcome is None or outcome.result() != game.headers["Result"]:
        raise ValueError("Результат не соответствует конечной позиции")
    return True


def export_own_games(directory: Path, destination: Path, *, candidate_only=False) -> int:
    lines = ["AI_CHESS_OWN_GAMES 2" if candidate_only else "AI_CHESS_OWN_GAMES 1"]
    count = 0
    for path in sorted(directory.glob("*.pgn")):
        for game in read_games(path):
            if not eligible_own_game(game):
                continue
            result = game.headers["Result"]
            color = ''
            if candidate_only:
                if (game.headers.get('FixedOpponentTraining') != 'true'
                        or game.headers.get('Experiment') != 'training'
                        or game.headers.get('CandidateColor') not in ('white', 'black')):
                    continue
                color = ' ' + game.headers['CandidateColor']
            lines.append(result + color + " " + " ".join(move.uci() for move in game.mainline_moves()))
            count += 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return count
