#pragma once
#include "board.hpp"
#include <atomic>
#include <chrono>
#include <functional>

namespace chess {
constexpr int MATE = 30000;
struct Limits {
    int depth = 64;
    int milliseconds = 1000;
};
struct SearchInfo {
    int depth = 0, score = 0;
    std::uint64_t nodes = 0;
    long long milliseconds = 0;
    std::vector<Move> pv;
};
struct Entry {
    Bits key = 0;
    Move move;
    std::int16_t score = 0;
    std::int8_t depth = -1;
    std::uint8_t bound = 0;
};
class Search {
  public:
    explicit Search(std::size_t megabytes = 16);
    void resize(std::size_t megabytes);
    void clear();
    Move run(Board board, Limits limits, std::atomic_bool &stop,
             const std::function<void(const SearchInfo &)> &report);

  private:
    std::vector<Entry> table;
    std::chrono::steady_clock::time_point start, deadline;
    std::atomic_bool *stop_flag = nullptr;
    std::uint64_t nodes = 0;
    bool interrupted = false, timed = false;
    Move root_best;
    int negamax(Board &board, int depth, int alpha, int beta, int ply);
    int quiescence(Board &board, int alpha, int beta, int ply, int quiet_checks = 2);
    bool stopped();
    Bits search_key(const Board &board) const;
    void order(const Board &board, MoveList &moves, Move preferred);
    std::vector<Move> principal_variation(Board board, int depth);
};
} // namespace chess
