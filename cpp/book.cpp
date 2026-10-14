#include "book.hpp"
#include <algorithm>
#include <cmath>
#include <fstream>
#include <limits>
#include <sstream>

namespace chess {
bool Book::load(const std::string &path, std::string &error) {
    std::ifstream in(path);
    std::string header;
    if (!std::getline(in, header) || header != "AI_CHESS_BOOK 1") {
        error = "Invalid book header";
        return false;
    }
    std::map<Bits, std::vector<BookRecord>> parsed;
    std::string line;
    while (std::getline(in, line)) {
        std::istringstream row(line);
        Bits key;
        unsigned move;
        BookRecord r;
        std::string extra;
        if (!(row >> key >> move >> r.wins >> r.draws >> r.losses) || (row >> extra) ||
            move > 65535 || !move || (move >> 12) > QUEEN) {
            error = "Invalid book record";
            return false;
        }
        r.move.value = static_cast<std::uint16_t>(move);
        parsed[key].push_back(r);
    }
    if (!in.eof()) {
        error = "Could not read book";
        return false;
    }
    entries = std::move(parsed);
    return true;
}
bool Book::save(const std::string &path) const {
    std::ofstream out(path);
    out << "AI_CHESS_BOOK 1\n";
    for (const auto &[key, records] : entries)
        for (const auto &r : records)
            out << key << ' ' << r.move.value << ' ' << r.wins << ' ' << r.draws << ' ' << r.losses
                << '\n';
    out.flush();
    return static_cast<bool>(out);
}
void Book::add(Bits key, Move move, int result) {
    auto &records = entries[key];
    auto it =
        std::find_if(records.begin(), records.end(), [&](const auto &r) { return r.move == move; });
    if (it == records.end()) {
        records.push_back({move, 0, 0, 0});
        it = records.end() - 1;
    }
    if (result > 0)
        ++it->wins;
    else if (result < 0)
        ++it->losses;
    else
        ++it->draws;
}
Move Book::probe(Board &b, std::mt19937_64 &rng) const {
    if (b.history.size() > 20 || b.draw())
        return {};
    auto it = entries.find(b.key());
    if (it == entries.end())
        return {};
    auto legal = b.legal_moves();
    std::vector<Move> moves;
    std::vector<double> weights;
    for (const auto &r : it->second)
        if (std::find(legal.begin(), legal.end(), r.move) != legal.end()) {
            double count = static_cast<double>(r.wins) + static_cast<double>(r.draws) +
                           static_cast<double>(r.losses);
            if (count < 1)
                continue;
            moves.push_back(r.move);
            weights.push_back(
                (2.0 * static_cast<double>(r.wins) + static_cast<double>(r.draws) + 1) /
                (count + 2) * std::sqrt(count));
        }
    if (moves.empty())
        return {};
    return moves[std::discrete_distribution<std::size_t>(weights.begin(), weights.end())(rng)];
}
} // namespace chess
