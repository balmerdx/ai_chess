#pragma once
#include <array>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace chess {
using Bits = std::uint64_t;
constexpr std::string_view START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
enum Piece { EMPTY, PAWN, KNIGHT, BISHOP, ROOK, QUEEN, KING };
struct Move {
    std::uint16_t value = 0;
    constexpr Move() = default;
    constexpr Move(int from, int to, int promotion = 0)
        : value(static_cast<std::uint16_t>(from | (to << 6) | (promotion << 12))) {}
    int from() const { return value & 63; }
    int to() const { return (value >> 6) & 63; }
    int promotion() const { return value >> 12; }
    bool operator==(const Move &) const = default;
    explicit operator bool() const { return value != 0; }
    std::string uci() const;
};
struct MoveList {
    std::array<Move, 256> moves{};
    int size = 0;
    void add(Move m) { moves[size++] = m; }
    Move *begin() { return moves.data(); }
    Move *end() { return moves.data() + size; }
    const Move *begin() const { return moves.data(); }
    const Move *end() const { return moves.data() + size; }
};
struct State {
    std::array<std::int8_t, 64> squares{};
    std::array<std::array<Bits, 7>, 2> pieces{};
    int side = 0, rights = 0, ep = -1, halfmove = 0, fullmove = 1;
};
class Board {
  public:
    State state;
    std::vector<Bits> history;
    Board();
    bool set_fen(std::string_view fen, std::string *error = nullptr);
    std::string fen() const;
    Bits key() const;
    bool attacked(int square, int by) const;
    bool in_check() const;
    MoveList legal_moves();
    Move parse_move(std::string_view text);
    State make(Move move);
    void unmake(const State &previous);
    bool is_capture(Move move) const;
    bool repeated(int count = 3) const;
    bool insufficient_material() const;
    bool draw() const;

  private:
    void put(int square, int piece);
    MoveList pseudo_moves() const;
};
std::uint64_t perft(Board &board, int depth);
int evaluate(const Board &board);
} // namespace chess
