#pragma once
#include "board.hpp"
#include <map>
#include <random>

namespace chess {
struct BookRecord {
    Move move;
    std::uint64_t wins = 0, draws = 0, losses = 0;
};
class Book {
  public:
    std::map<Bits, std::vector<BookRecord>> entries;
    bool load(const std::string &path, std::string &error);
    bool save(const std::string &path) const;
    void add(Bits key, Move move, int result);
    Move probe(Board &board, std::mt19937_64 &rng) const;
};
} // namespace chess
